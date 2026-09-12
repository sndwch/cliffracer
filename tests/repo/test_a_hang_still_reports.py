"""Verify pytest summary reporting when a test times out."""

import pathlib
import re
import subprocess
import sys
import tomllib

import pytest

pytestmark = pytest.mark.repo

REPO = pathlib.Path(__file__).resolve().parents[2]
SUMMARY = re.compile(r"^\d+ (passed|failed)|\d+ failed", re.M)

HANGS = """
import time


def test_this_one_never_returns():
    time.sleep(30)


def test_this_one_is_fine():
    assert True
"""


def configured_timeout_method() -> str:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    return cfg["tool"]["pytest"]["ini_options"]["timeout_method"]


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


def test_CONTROL_the_thread_method_really_does_destroy_the_report(tmp_path):
    """Verify thread-based timeout method does not produce a summary line."""
    out = (lambda r: r.stdout + r.stderr)(_run(tmp_path, "thread"))
    assert not SUMMARY.search(out), (
        "the thread method now yields a summary too; this control no longer "
        f"distinguishes the two and the test above is unsupported:\n{out[-3000:]}"
    )
