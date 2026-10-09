"""The autouse correlation_id_var reset fixture cleans ambient context between tests, and names a leak.

The fixture is read by the suite's own tests here: the canary pair shows a set in one test is gone
in the next, and a run of the real fixture over small test files shows what it does to a test that
leaks, to one that says it leaks on purpose, and to the test that follows.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import cliffracer
from cliffracer.core.correlation import correlation_id_var, set_correlation_id

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]

#: The `src` directory this process imported `cliffracer` from. A child is pointed at it, so it runs
#: the code under test: `REPO / "src"` is the tree this file sits in, which is another tree's code
#: when one tree's tests are run against another tree's `src`.
IMPORTED_SRC = str(Path(cliffracer.__file__).resolve().parents[1])


@pytest.mark.leaves_correlation_id
def test_correlation_canary_step_1_set_contextvar():
    """Set canary correlation ID in ambient context, on purpose: step 2 reads that it is gone."""
    set_correlation_id("canary_isolation_token_active")
    assert correlation_id_var.get() == "canary_isolation_token_active"


def test_correlation_canary_step_2_verify_reset():
    """Verify the previous test context was cleared by the autouse fixture."""
    assert correlation_id_var.get() is None, (
        "correlation_id_var was not reset by autouse fixture between tests"
    )


PROBE = """
import pytest
from cliffracer.core.correlation import correlation_id_var, set_correlation_id


def test_a_leaks():
    set_correlation_id("leaked-by-a")


def test_b_follows_a_leak():
    assert correlation_id_var.get() is None


@pytest.mark.leaves_correlation_id
def test_c_leaks_on_purpose():
    set_correlation_id("left-by-c")


def test_d_follows_c():
    assert correlation_id_var.get() is None


def test_e_leaks_nothing():
    pass
"""


def _run_the_real_fixture(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """The suite's own fixture, imported from tests/conftest.py, over a probe file."""
    # A plugin, not a conftest: tests/conftest.py imports the root `conftest` by that name.
    (tmp_path / "leak_check.py").write_text(
        "from tests.conftest import _reset_correlation_id_var  # noqa: F401\n"
    )
    (tmp_path / "pytest.ini").write_text(
        textwrap.dedent(
            """
            [pytest]
            markers = leaves_correlation_id: the test leaves the id set on purpose
            """
        )
    )
    (tmp_path / "test_probe.py").write_text(PROBE)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "leak_check",
            "-rA",
            "--tb=line",
            "-q",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_fixture_env(tmp_path),
        timeout=120,
    )


def _fixture_env(tmp_path: Path) -> dict[str, str]:
    """The child pytest's environment: the probe files, the repository root (for `tests.conftest`)
    and the imported `src`."""
    return {
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(REPO), IMPORTED_SRC]),
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }


def test_the_fixtures_child_imports_the_cliffracer_this_process_imported(tmp_path: Path):
    done = subprocess.run(
        [sys.executable, "-c", "import cliffracer, sys; sys.stdout.write(cliffracer.__file__)"],
        capture_output=True,
        text=True,
        env=_fixture_env(tmp_path),
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert Path(done.stdout).resolve().parents[1] == Path(IMPORTED_SRC), (done.stdout, IMPORTED_SRC)


def test_the_fixture_names_the_test_that_leaks_and_spares_the_one_that_says_so(tmp_path: Path):
    result = _run_the_real_fixture(tmp_path)
    out = result.stdout + result.stderr

    # Exactly one error, at the teardown of the leaking test, naming the id.
    assert "1 error" in out and "5 passed" in out, out
    assert "ERROR test_probe.py::test_a_leaks" in out, out
    assert "this test left correlation_id_var set to 'leaked-by-a'" in out, out
    # The one marked as leaving it is not reported, and the leaks never reach the next test.
    assert "ERROR test_probe.py::test_c_leaks_on_purpose" not in out, out
    for name in ("test_b_follows_a_leak", "test_d_follows_c", "test_e_leaks_nothing"):
        assert f"PASSED test_probe.py::{name}" in out, out
