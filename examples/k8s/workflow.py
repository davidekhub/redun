import sys
import os
import subprocess
from typing import Dict

from redun import File, script, task


redun_namespace = "redun.examples.k8s"

@task(executor='k8s')
def task_on_k8s() -> list:
    return [
        'task_on_k8s',
        print("hello stdout"),
        print("hello stderr", file=sys.stderr),
    ]

@task()
def script_on_k8s() -> File:
   # The outer task is just for preparing the script and its arguments.
    return script(  # This inner task will run as a bash script inside the container.
        f"ls /", 
        executor="k8s",
        outputs=File("-"),
    )

@task(executor='k8s')
def failed_task_on_k8s() -> list:
    raise RuntimeError


@task(executor='batch')
def task_on_batch() -> list:
    return [
        'task_on_batch',
        print("hello stdout"),
        print("hello stderr", file=sys.stderr),
    ]


@task()
def main() -> list:
    # This is the top-level task of the workflow. Here, we are invoking the
    # different examples of running tasks on different executors. All of their
    # results will be combined into one nested list as shown below.
    return [
        'main',
        #task_on_k8s(),
        script_on_k8s(),
        #failed_task_on_k8s(),
        #task_on_batch(),
    ]
