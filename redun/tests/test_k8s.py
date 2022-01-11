import pickle
from unittest.mock import Mock, patch
import boto3
import pytest
import uuid
from kubernetes import client
from moto import mock_logs, mock_s3

import redun.executors.k8s
from redun import File, task
from redun.cli import RedunClient, import_script
from redun.config import Config
from redun.tests.utils import mock_scheduler
from redun.executors.k8s_utils import create_job_object
from redun.executors.k8s import (
    K8SExecutor,
    DockerResult,
    k8s_submit,
    get_k8s_job_name,
    get_hash_from_job_name,
    iter_k8s_job_log_lines,
    iter_k8s_job_logs,
    parse_task_error,
    submit_task,
)
from redun.executors.aws_utils import (
    REDUN_REQUIRED_VERSION,
    create_tar,
    extract_tar,
    find_code_files,
    get_job_scratch_file,
    package_code,
    parse_code_package_config,
)
from redun.file import Dir
from redun.scheduler import Execution, Job, Scheduler, Traceback
from redun.tests.utils import mock_scheduler, use_tempdir, wait_until
from redun.utils import pickle_dumps

# skipped job_def tests here

# TODO(dek): figure out why the suffix isn't "
@pytest.mark.parametrize("suffix", ["c000d7f9b6275c58aff9d5466f6a1174e99195ca"])
def test_get_hash_from_job_name(suffix) -> None:
    """
    Returns the Job hash from a k8s job name.
    """
    prefix = "my-job-prefix"
    job_hash = "c000d7f9b6275c58aff9d5466f6a1174e99195ca"
    job_name = get_k8s_job_name(prefix, job_hash)
    assert job_name.startswith(prefix)
    assert job_name.endswith(suffix)

    job_hash2 = get_hash_from_job_name(job_name)
    assert job_hash2 == job_hash


def mock_executor(scheduler, code_package=False):
    """
    Returns an K8SExecutor with AWS API mocks.
    """
    image = "my-image"
    s3_scratch_prefix = "s3://example-bucket/redun/"

    # Setup executor.
    config = Config(
        {
            "k8s": {
                "image": image,
                "s3_scratch": s3_scratch_prefix,
                "job_monitor_interval": 0.05,
                "job_stale_time": 0.01,
                "code_package": code_package,
            }
        }
    )
    executor = K8SExecutor("k8s", scheduler, config["k8s"])

    executor.get_jobs = Mock()
            
    executor.get_jobs.return_value = client.V1JobList(items=[])

    s3_client = boto3.client("s3", region_name="us-east-1")
    s3_client.create_bucket(Bucket="example-bucket")

    return executor


# skipped batch tags tests here

def test_executor_config(scheduler: Scheduler) -> None:
    """
    Executor should be able to parse its config.
    """
    # Setup executor.
    config = Config(
        {
            "k8s": {
                "image": "image",
                "s3_scratch": "s3_scratch_prefix",
                "code_includes": "*.txt",
            }
        }
    )
    executor = K8SExecutor("k8s", scheduler, config["k8s"])

    assert executor.image == "image"
    assert executor.s3_scratch_prefix == "s3_scratch_prefix"
    assert isinstance(executor.code_package, dict)
    assert executor.code_package["includes"] == ["*.txt"]


@task()
def task1(x):
    return x + 10

@task(load_module="custom.module")
def task1_custom_module(x):
    return x + 10



@use_tempdir
@mock_s3
@patch("redun.executors.k8s.k8s_submit")
@pytest.mark.parametrize(
    "custom_module, expected_load_module, a_task",
    [
        (None, "redun.tests.test_k8s", task1),
        ("custom.module", "custom.module", task1_custom_module),
    ],
)
def test_submit_task(k8s_submit_mock, custom_module, expected_load_module, a_task):
    job_id = "123"
    image = "my-image"
    s3_scratch_prefix = "s3://example-bucket/redun/"

    s3_client = boto3.client("s3", region_name="us-east-1")
    s3_client.create_bucket(Bucket="example-bucket")

    redun.executors.k8s.k8s_submit.return_value = create_job_object(uid=job_id)

    # Create example workflow script to be packaged.
    File("workflow.py").write(
        f"""
@task(load_module={custom_module})
def task1(x):
    return x + 10
    """
    )

    job = Job(a_task())
    job.id = job_id
    job.eval_hash = "eval_hash"
    code_file = package_code(s3_scratch_prefix)
    resp = submit_task(
        image,
        s3_scratch_prefix,
        job,
        a_task,
        args=[10],
        kwargs={},
        code_file=code_file,
    )
    # # We should get a k8s job id back.
    assert resp.metadata.uid == job_id

    # # Input files should be made.
    assert File("s3://example-bucket/redun/jobs/eval_hash/input").exists()
    [code_file] = list(Dir("s3://example-bucket/redun/code"))

    # We should have submitted a job to K8S.
    redun.executors.k8s.k8s_submit.assert_called_with(
        [
            "redun",
            "--check-version",
            REDUN_REQUIRED_VERSION,
            "oneshot",
            expected_load_module,
            "--code",
            code_file.path,
            "--input",
            "s3://example-bucket/redun/jobs/eval_hash/input",
            "--output",
            "s3://example-bucket/redun/jobs/eval_hash/output",
            "--error",
            "s3://example-bucket/redun/jobs/eval_hash/error",
            a_task.name,
        ],
        image="my-image",
        job_name="k8s-job-eval_hash",
    )


@use_tempdir
@mock_s3
@patch("redun.executors.k8s.k8s_submit")
def test_submit_task_deep_file(k8s_submit_mock):
    """
    Executor should be able to submit a task defined in a deeply nested file path.
    """
    job_id = "123"
    image = "my-image"
    s3_scratch_prefix = "s3://example-bucket/redun/"

    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="example-bucket")

    redun.executors.k8s.k8s_submit.return_value = {"jobId": "k8s-job-id"}

    # Create example workflow script to be packaged.
    File("path/to/workflow.py").write(
        """
from redun import task

@task()
def task1(x):
    return x + 10
    """
    )

    module = import_script("path/to/workflow.py")

    job = Job(module.task1())
    job.id = job_id
    job.eval_hash = "eval_hash"
    code_file = package_code(s3_scratch_prefix)
    resp = submit_task(
        image,
        s3_scratch_prefix,
        job,
        module.task1,
        args=[10],
        kwargs={},
        code_file=code_file,
    )

    # We should get a k8s job id back.
    assert resp["jobId"] == "k8s-job-id"

    # Input files should be made.
    assert File("s3://example-bucket/redun/jobs/eval_hash/input").exists()
    [code_file] = list(Dir("s3://example-bucket/redun/code"))

    # We should have submitted a job to k8s.
    redun.executors.k8s.k8s_submit.assert_called_with(
        [
            "redun",
            "--check-version",
            REDUN_REQUIRED_VERSION,
            "oneshot",
            "workflow",
            "--import-path",
            "path/to",
            "--code",
            code_file.path,
            "--input",
            "s3://example-bucket/redun/jobs/eval_hash/input",
            "--output",
            "s3://example-bucket/redun/jobs/eval_hash/output",
            "--error",
            "s3://example-bucket/redun/jobs/eval_hash/error",
            "task1",
        ],
        image="my-image",
        job_name="k8s-job-eval_hash",
    )

@mock_s3
@patch("redun.executors.k8s.parse_task_logs")
@patch("redun.executors.k8s.iter_k8s_job_status")
@patch("redun.executors.k8s.k8s_submit")
def test_executor(
    k8s_submit_mock, iter_k8s_job_status_mock, parse_task_logs_mock
) -> None:
    """
    Ensure that we can submit job to K8SExecutor.
    """
    k8s_job_id = "k8s-job-id"
    k8s_job2_id = "k8s-job2-id"

    # Setup K8S mocks.
    iter_k8s_job_status_mock.return_value = iter([])
    parse_task_logs_mock.return_value = []

    scheduler = mock_scheduler()
    executor = mock_executor(scheduler)
    executor.start()

    # DO NOT SUBMIT: DRY the job name
    k8s_submit_mock.return_value = create_job_object(name="redun-job-eval_hash", uid=k8s_job_id)
    # Submit redun job that will succeed.
    expr = task1(10)
    job = Job(expr)
    job.task = task1
    job.eval_hash = "eval_hash"
    executor.submit(job, [10], {})

    # Let job get stale so job arrayer actually submits it.
    wait_until(lambda: executor.arrayer.num_pending == 0, timeout=1000)
    # # # Ensure job options were passed correctly.
    assert k8s_submit_mock.call_args
    # DO NOT SUBMIT: DRY the job name
    assert k8s_submit_mock.call_args[1] == {
        "image": "my-image",
        "job_name": "redun-job-eval_hash",
        "vcpus": 1,
        "gpus": 0,
        "memory": 4,
        "role": None,
        "retries": 1,
        "k8s_labels": {
            'redun_aws_user': '',
            'redun_execution_id': '',
            'redun_job_id': job.id,
            'redun_project': '',
            'redun_task_name': 'task1'
        }
    }

    # DO NOT SUBMIT: DRY the job name
    k8s_submit_mock.return_value = create_job_object(name="redun-job-eval_hash2", uid=k8s_job2_id)

    # Submit redun job that will fail.
    expr2 = task1.options(memory=8)("a")
    job2 = Job(expr2)
    job2.task = task1
    job2.eval_hash = "eval_hash2"
    executor.submit(job2, ["a"], {})

    # # # Let job get stale so job arrayer actually submits it.
    wait_until(lambda: executor.arrayer.num_pending == 0)

    # # # Ensure job options were passed correctly.
    # DO NOT SUBMIT: compute the job name properly
    assert k8s_submit_mock.call_args[1] == {
        "image": "my-image",
        "job_name": "redun-job-eval_hash2",
        "vcpus": 1,
        "gpus": 0,
        "memory": 8,
        "role": None,
        "retries": 1,
        "k8s_labels": {
            'redun_aws_user': '',
            'redun_execution_id': '',
            'redun_job_id': job2.id,
            'redun_project': '',
            'redun_task_name': 'task1'
            }
        }

    # # Simulate k8s completing job.
    output_file = File("s3://example-bucket/redun/jobs/eval_hash/output")
    output_file.write(pickle_dumps(task1.func(10)), mode="wb")

    # # Simulate k8s failing.
    error = ValueError("Boom")
    error_file = File("s3://example-bucket/redun/jobs/eval_hash2/error")
    error_file.write(pickle_dumps((error, Traceback.from_error(error))), mode="wb")

    fake_k8s_job = create_job_object(uid=k8s_job_id, name="redun-job-eval_hash")
    fake_k8s_job.status = client.V1JobStatus(succeeded=1)
    fake_k8s_job2 = create_job_object(uid=k8s_job2_id, name="redun-job-eval_hash2")
    fake_k8s_job2.status = client.V1JobStatus(failed=1)
    iter_k8s_job_status_mock.return_value = iter([fake_k8s_job, fake_k8s_job2])
    
    scheduler.batch_wait([job.id, job2.id])
    executor.stop()

    # # Job results and errors should be sent back to scheduler.
    assert scheduler.job_results[job.id] == 20
    assert isinstance(scheduler.job_errors[job2.id], ValueError)

    # # Assert job tags.
    job.job_tags == [("k8s_job", "k8s-job-id"), ("aws_log_stream", "log1")]
    job.job_tags == [("k8s_job", "k8s-job2-id"), ("aws_log_stream", "log2")]

# skip test_executor_docker here

# skip test_executor_error_override here
# skip test_executor_multi_start here

# skipped docker interactive

@mock_s3
def test_executor_handles_unrelated_jobs() -> None:
    """
    Regression test for https://insitro.atlassian.net/browse/DE-2632

    There is an expanding pattern of using a "headnode" running in batch to trigger redun
    pipelines. If the headnode and redun jobs that it spawns have a shared job_name_prefix then
    the headnode job can get gathered in the `get_jobs` call and we will try to extract the hash.
    However, since the headnode job is not a redun job, it will not have a hash and previously
    caused execution failures.

    This test confirms that jobs without hashes in their names are ignored which allows headnode
    jobs(triggered via lambda or otherwise) to share job name prefixes with the redun jobs that
    they spawn.
    """
    scheduler = mock_scheduler()
    executor = mock_executor(scheduler)

    prefix = "liveratlas_spearmancor"
    hash1 = "123456789"
    hash2 = "987654321"

    # Set up mocks to include a headnode job(no hash) and some redun jobs that it "spawned".
    executor.get_jobs.return_value = client.V1JobList(items=[
        create_job_object(uid='headnode', name=f"{prefix}_automation_headnode"),
        create_job_object(uid='preprocess', name=f"{prefix}_preprocess-{hash1}"),
        create_job_object(uid='decode', name=f"{prefix}_decode-{hash2}")])

    executor.gather_inflight_jobs()

    assert executor.preexisting_k8s_jobs == {
        hash1: "preprocess",
        hash2: "decode",
    }

@mock_s3
@patch("redun.executors.aws_utils.get_aws_user", return_value="alice")
@patch("redun.executors.aws_utils.package_code")
def test_code_packaging(package_code_mock, get_aws_user_mock) -> None:
    """
    Ensure that code packaging only happens on first submission.
    """
    package_code_mock.return_value = "s3://fake-bucket/code.tar.gz"

    scheduler = mock_scheduler()
    executor = mock_executor(scheduler, code_package=True)
    executor.start()

    # Starting the executor should not have triggered code packaging.
    assert executor.code_file is None
    assert package_code_mock.call_count == 0

    # Hand create jobs.
    job1 = Job(task1(10))
    job1.id = "1"
    job1.task = task1
    job1.eval_hash = "eval_hash"

    job2 = Job(task1(20))
    job2.id = "2"
    job2.task = task1
    job2.eval_hash = "eval_hash"

    # Submit a job and ensure that the code was packaged.
    executor.submit(job1, [10], {})
    assert executor.code_file == "s3://fake-bucket/code.tar.gz"
    assert package_code_mock.call_count == 1

    # Submit another job and ensure that code was not packaged again.
    executor.submit(job2, [20], {})
    assert package_code_mock.call_count == 1

    executor.stop()


@mock_s3
@patch("redun.executors.aws_utils.get_aws_user", return_value="alice")
@patch("redun.executors.k8s.k8s_describe_jobs")
def test_inflight_join_only_on_first_submission(k8s_describe_jobs_mock, get_aws_user_mock) -> None:
    """
    Ensure that inflight jobs are only gathered once and not on every job submission.
    """
    scheduler = mock_scheduler()
    executor = mock_executor(scheduler)

    executor.start()

    # Hand create jobs.
    job1 = Job(task1(10))
    job1.id = "1"
    job1.task = task1
    job1.eval_hash = "eval_hash"

    job2 = Job(task1(20))
    job2.id = "2"
    job2.task = task1
    job2.eval_hash = "eval_hash"

    # Submit redun job.
    executor.submit(job1, [10], {})

    # Ensure that inflight jobs were gathered.
    assert executor.get_jobs.call_count == 1

    # Submit the second job and confirm that job reuniting was not done again.
    executor.submit(job2, [20], {})
    assert executor.get_jobs.call_count == 1

    executor.stop()


@mock_s3
@patch("redun.executors.aws_utils.get_aws_user", return_value="alice")
@patch("redun.executors.k8s.k8s_describe_jobs")
@patch("redun.executors.k8s.iter_k8s_job_status")
@patch("redun.executors.k8s.k8s_submit")
def test_executor_inflight_job(
    k8s_submit_mock,
    iter_k8s_job_status_mock,
    k8s_describe_jobs_mock,
    get_aws_user_mock,
) -> None:
    """
    Ensure we reunite with an inflight job.
    """
    k8s_job_id = "333"

    # Setup k8s mocks.
    
    iter_k8s_job_status_mock.return_value = iter([])
                    # DO NOT SUBMIT: DRY

    k8s_describe_jobs_mock.return_value = [create_job_object(uid=k8s_job_id, name='redun-job-eval_hash')]

    scheduler = mock_scheduler()
    executor = mock_executor(scheduler)
    executor.get_jobs.return_value = client.V1JobList(items=[
                 # DO NOT SUBMIT: DRY
           create_job_object(uid=k8s_job_id, name='redun-job-eval_hash')])
    executor.start()

    # Hand create job.
    job = Job(task1(10))
    job.id = "123"
    job.task = task1
    job.eval_hash = "eval_hash"

    # Submit redun job.
    executor.submit(job, [10], {})

    # Ensure no k8s jobs were submitted.
    assert k8s_submit_mock.call_count == 0

    # Simulate K8S completing with valid value.
    output_file = File("s3://example-bucket/redun/jobs/eval_hash/output")
    output_file.write(pickle_dumps(task1.func(10)), mode="wb")

    k8s_job = create_job_object(uid=k8s_job_id, name='redun-job-eval_hash')
    k8s_job.status = client.V1JobStatus(succeeded=1)
    iter_k8s_job_status_mock.return_value = [k8s_job]
    scheduler.batch_wait([job.id])
    # Simulate pre-existing job output.
    output_file = File("s3://example-bucket/redun/jobs/eval_hash/output")
    output_file.write(pickle_dumps(task1.func(10)), mode="wb")

    # Ensure redun job is completed.
    assert scheduler.job_results[job.id] == 20

    executor.stop()


# skipped job array tests

if __name__ == '__main__':
    test_executor_inflight_job()