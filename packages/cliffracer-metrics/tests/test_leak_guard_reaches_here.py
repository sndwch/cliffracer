"""Verify the leaked task guard fixture applies to package tests and detects leaked tasks."""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

GUARD = "_no_leaked_tasks"
PACKAGE_TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_leaked_task_guard_applies_to_package_tests(request):
    """Verify the autouse fixture name is present in package test fixtures."""
    assert GUARD in request.fixturenames, f"{GUARD} must be active for packages/"


def test_the_leaked_task_guard_catches_unclosed_asyncio_task():
    """Verify that a package test leaving a running background task fails during fixture teardown."""
    leaking_source = (
        "import asyncio\n"
        "import pytest\n"
        "pytestmark = pytest.mark.unit\n\n"
        "async def test_leaks_background_task():\n"
        "    asyncio.create_task(asyncio.sleep(60))\n"
    )
    with tempfile.TemporaryDirectory(dir=PACKAGE_TESTS_DIR) as tmp_dir:
        probe_file = Path(tmp_dir) / "test_leak_probe.py"
        probe_file.write_text(leaking_source)

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(probe_file),
                "-p",
                "no:cacheprovider",
                "-q",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        assert proc.returncode != 0
        output = proc.stdout + proc.stderr
        assert "left 1 task(s) running" in output
        assert "test_leaks_background_task" in output


def test_CONTROL_the_check_reads_the_real_fixture_list(request):
    """Verify request.fixturenames distinguishes present and absent fixtures."""
    assert "_not_a_fixture_anybody_defined" not in request.fixturenames
    assert "request" in request.fixturenames
