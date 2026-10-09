"""A pytest the job's memory cap kills says so, and its step still fails with pytest's status.

A SIGKILLed process exits 137 and prints nothing, so a step that stops there reads as a bare
failure. Gitea's job containers run under a memory cap (the runner's `--memory`), which is the
usual sender of that SIGKILL here, so each Gitea "Run tests" step names it on 137. The steps are
run as the runner runs them, `bash -e`, with a stand-in `uv` that ends the way pytest would.
"""

import os
import subprocess
from pathlib import Path

import pytest

from tests.repo.ci_workflows import ci_workflow_files, load

pytestmark = pytest.mark.repo

MESSAGE = (
    "pytest was killed by SIGKILL (exit 137), most likely the job container's memory cap "
    "(see the runner's --memory)"
)
ENDINGS = {"killed": ("kill -9 $$", 137), "failed": ("exit 1", 1), "passed": ("exit 0", 0)}


def run_test_steps() -> list[object]:
    (workflow,) = ci_workflow_files("gitea")
    steps = [
        pytest.param(job_id, step["run"], id=job_id)
        for job_id, job in load(workflow)["jobs"].items()
        for step in job.get("steps", [])
        if step.get("name") == "Run tests"
    ]
    assert steps, f"no 'Run tests' step in {workflow}"
    return steps


@pytest.mark.parametrize("ending", sorted(ENDINGS))
@pytest.mark.parametrize(("job_id", "script"), run_test_steps())
def test_a_run_tests_step_names_a_sigkill_and_keeps_pytests_status(
    tmp_path: Path, job_id: str, script: str, ending: str
):
    action, status = ENDINGS[ending]
    uv = tmp_path / "uv"
    uv.write_text(f"#!/bin/bash\n{action}\n")
    uv.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        ["bash", "-e", "-c", script], capture_output=True, text=True, env=env, timeout=30
    )
    assert result.returncode == status, (job_id, result.stdout, result.stderr)
    assert (MESSAGE in result.stdout) == (status == 137), (job_id, result.stdout)
