"""A packaging guard's `uv build` without the package index builds from the cache, or says so.

`tests/repo/built_distributions.uv_build` is the build every packaging guard runs. Run with no
network, `uv build` fails reaching the index for the build backend, which read as "uv build
failed", a packaging defect, so reviewers learned to set those reds aside. It now tries again
offline, from uv's cache, and when the cache cannot serve the backend either, the failure names
the index and the cache rather than the build.

The outputs below are uv 0.12.8's own, captured from runs inside a network namespace with uv's
cache warm and cold; the runner standing in for `subprocess.run` replays them in order.
"""

import subprocess
from pathlib import Path

import pytest

from tests.repo import built_distributions
from tests.repo.built_distributions import uv_build

pytestmark = pytest.mark.repo

BUILT = "Building source distribution...\nSuccessfully built dist/cliffracer-1.2.1.tar.gz\n"
UNREACHABLE = (
    "  Caused by: error sending request for url (https://pypi.org/simple/hatch-vcs/)\n"
    "  Caused by: client error (Connect)\n"
    "  Caused by: dns error\n"
    "  Caused by: failed to lookup address information: Name or service not known\n"
)
NOT_CACHED = (
    "  Caused by: No solution found when resolving: `hatchling`, `hatch-vcs`\n"
    "  Caused by: Because hatchling was not found in the cache and you require hatchling, we can "
    "conclude that your requirements are unsatisfiable.\n"
    "hint: Packages were unavailable because the network was disabled. When the network is "
    "disabled, registry packages may only be read from the cache.\n"
)
BROKEN = "  × Failed to build `cliffracer`\n  ╰─▶ The build backend returned an error\n"


@pytest.fixture
def replay(monkeypatch):
    """Answers each `uv build` with the next (returncode, stderr), and records the commands."""
    commands: list[list[str]] = []
    answers: list[tuple[int, str]] = []

    def run(command, cwd, capture_output, text):
        commands.append(command)
        assert answers, f"uv build was run more times than this case expects: {commands}"
        code, stderr = answers.pop(0)
        return subprocess.CompletedProcess(command, code, BUILT if code == 0 else "", stderr)

    monkeypatch.setattr(built_distributions.subprocess, "run", run)
    return commands, answers


def test_a_build_that_succeeds_is_run_once(replay):
    commands, answers = replay
    answers.append((0, ""))

    uv_build(["--sdist"], Path("."))

    assert commands == [["uv", "build", "--sdist"]]


def test_a_build_that_cannot_reach_the_index_is_built_from_the_cache(replay):
    commands, answers = replay
    answers.extend([(2, UNREACHABLE), (0, "")])

    proc = uv_build(["--sdist"], Path("."))

    assert proc.returncode == 0
    assert commands == [["uv", "build", "--sdist"], ["uv", "build", "--offline", "--sdist"]]


def test_a_build_the_cache_cannot_serve_either_names_the_index_and_the_cache(replay):
    _, answers = replay
    answers.extend([(2, UNREACHABLE), (2, NOT_CACHED)])

    with pytest.raises(AssertionError) as failed:
        uv_build(["--sdist"], Path("."))

    said = str(failed.value)
    assert "could not reach the package index" in said and "uv's cache does not hold it" in said
    assert "not the packaging" in said
    assert not said.startswith("uv build failed")


def test_an_offline_run_on_a_cold_cache_names_the_cache_without_trying_again(replay):
    """`UV_OFFLINE=1` on a cold cache: uv never reaches for the index, and says the cache lacks it."""
    commands, answers = replay
    answers.append((2, NOT_CACHED))

    with pytest.raises(AssertionError, match="could not reach the package index"):
        uv_build(["--sdist"], Path("."))

    assert commands == [["uv", "build", "--sdist"]]


def test_CONTROL_a_build_that_fails_for_itself_is_a_build_failure_and_is_not_retried(replay):
    commands, answers = replay
    answers.append((1, BROKEN))

    with pytest.raises(AssertionError) as failed:
        uv_build(["--sdist"], Path("."))

    assert str(failed.value).startswith("uv build failed")
    assert commands == [["uv", "build", "--sdist"]]
