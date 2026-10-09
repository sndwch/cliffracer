"""Verify pytest still reports a summary when a test times out.

Two settings decide that. `timeout_method` decides whether the runner survives
the interruption at all, and `timeout` decides whether a blocked call is ever
interrupted in the first place. Both are read out of pyproject.toml here, and
both are exercised rather than pinned as literals.
"""

import pathlib
import re
import subprocess
import sys
import tomllib

import pytest

pytestmark = pytest.mark.repo

REPO = pathlib.Path(__file__).resolve().parents[2]
SUMMARY = re.compile(r"^\d+ (passed|failed)|\d+ failed", re.M)

# Longer than any timeout this file drives a subprocess with, so the sleep is
# always the thing being interrupted rather than the thing that finishes first.
HANGS = """
import time


def test_this_one_never_returns():
    time.sleep(30)


def test_this_one_is_fine():
    assert True
"""


def _ini_options() -> dict:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    return cfg["tool"]["pytest"]["ini_options"]


def configured_timeout_method() -> str:
    return str(_ini_options()["timeout_method"])


def configured_timeout() -> float:
    """Return the project's per-test timeout ceiling in seconds."""
    return float(_ini_options()["timeout"])


def _run(tmp_path: pathlib.Path, method: str) -> subprocess.CompletedProcess:
    (tmp_path / "test_hangs.py").write_text(HANGS)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path / "test_hangs.py"),
            "-p",
            "no:cacheprovider",
            "-q",
            "--timeout=2",
            f"--timeout-method={method}",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _run_under_ini(
    tmp_path: pathlib.Path, ini_body: str, wait: float
) -> subprocess.CompletedProcess | None:
    """Run the hang with `ini_body` as the whole pytest config, no timeout flags.

    Returns None if the run was still going after `wait` seconds, which is what
    an uninterrupted hang looks like from outside.
    """
    (tmp_path / "test_hangs.py").write_text(HANGS)
    (tmp_path / "pytest.ini").write_text(ini_body)
    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(tmp_path / "test_hangs.py"),
                "-p",
                "no:cacheprovider",
                "-q",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=wait,
        )
    except subprocess.TimeoutExpired:
        return None


def test_the_project_configures_a_finite_per_test_timeout() -> None:
    """A per-test ceiling is configured, so a blocked call is interrupted.

    Without this value pytest-timeout never fires: a hung test blocks forever,
    no summary line is produced and no failure is named.
    """
    options = _ini_options()
    assert "timeout" in options, (
        "[tool.pytest.ini_options] configures no `timeout`, so a blocked call "
        "hangs the suite forever instead of being interrupted and reported"
    )
    timeout = configured_timeout()
    assert timeout > 0, f"the configured per-test timeout is {timeout}, which never fires"


def test_the_project_uses_a_timeout_method_that_survives_a_hang(tmp_path):
    """Read from pyproject and exercised, rather than asserted as a string.

    Pinning the literal "signal" would pass on a project where the setting had
    stopped working. This takes whatever the project configures and makes a
    test hang under it.
    """
    result = _run(tmp_path, configured_timeout_method())
    out = result.stdout + result.stderr
    assert SUMMARY.search(out), (
        "a hang under the project's configured timeout_method left no summary "
        f"line, so a real run would report no count and no failure name:\n{out[-3000:]}"
    )
    assert "test_this_one_never_returns" in out, out[-3000:]


def test_a_configured_timeout_key_is_what_interrupts_the_hang(tmp_path):
    """An ini `timeout` alone, with no command-line flag, interrupts and reports.

    The project drives its own suite from the ini, never from `--timeout`, so
    this is the path that has to work.
    """
    ini = f"[pytest]\ntimeout = 2\ntimeout_method = {configured_timeout_method()}\n"
    result = _run_under_ini(tmp_path, ini, wait=60)
    assert result is not None, "the ini timeout never fired; the hang ran to the deadline"
    out = result.stdout + result.stderr
    assert SUMMARY.search(out), out[-3000:]
    assert "test_this_one_never_returns" in out, out[-3000:]


def test_CONTROL_without_the_timeout_key_the_hang_is_never_interrupted(tmp_path):
    """Drop only `timeout` from the ini above and the hang runs on untouched.

    This is what makes the previous test load-bearing: it shows the reporting it
    observes comes from the configured value and not from something else.
    """
    ini = f"[pytest]\ntimeout_method = {configured_timeout_method()}\n"
    result = _run_under_ini(tmp_path, ini, wait=8)
    assert result is None, (
        "a hang finished without any configured timeout, so the test above no "
        "longer shows that the `timeout` key is what interrupts it:\n"
        f"{(result.stdout + result.stderr)[-3000:]}"
    )


def test_CONTROL_the_thread_method_really_does_destroy_the_report(tmp_path):
    """Verify thread-based timeout method does not produce a summary line.

    An absence is read only after the run is shown to have reached the hang: the timeout banner
    and the hanging test in the stack it prints. A run that failed before collecting anything (a
    bad `--timeout-method`, a pytest that will not start) has no summary line either.
    """
    out = (lambda r: r.stdout + r.stderr)(_run(tmp_path, "thread"))
    assert "+ Timeout +" in out and "in test_this_one_never_returns" in out, (
        f"the run did not reach the hanging test, so the absence below proves nothing:\n{out[-3000:]}"
    )
    assert not SUMMARY.search(out), (
        "the thread method now yields a summary too; this control no longer "
        f"distinguishes the two and the test above is unsupported:\n{out[-3000:]}"
    )
