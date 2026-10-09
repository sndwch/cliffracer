"""A test that configures logging does not change what the next test reads from loguru's `extra`.

`LoggingConfig.configure` merges `service` into loguru's one process-wide `extra`. Tests in several
files call it and put nothing back, so a later test that read `extra["service"]` saw whichever test
had configured logging last, and passed or failed on the order the tests ran in. `conftest.py` now
restores the extra after every test.

The check runs a leaking test and a reading test in a pytest of its own, with the root `conftest.py`
loaded and without it: without it the reader fails, which is what the package's own tests did. (A
`conftest.py` inside a package's `tests/` cannot do this: when `tests/` and `packages/` are collected
together its module name collides with `tests/conftest.py`.)
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[3]
FIXTURE = "_a_test_leaves_the_process_wide_loguru_extra_as_it_found_it"

HOST = """
import pytest
from loguru import logger


@pytest.fixture(scope="session", autouse=True)
def _a_host_that_set_its_own_extra():
    logger.configure(extra={"app": "myapp"})
    yield
"""

TESTS = """
from loguru import logger
from cliffracer_logging import LoggingConfig


def _extra():
    return dict(logger._core.extra)


def test_1_a_test_that_configures_logging_and_puts_nothing_back():
    LoggingConfig.configure(service_name="leaker", enable_console=False, enable_file=False)
    assert _extra()["service"] == "leaker"


def test_2_the_next_test_reads_what_the_host_set():
    assert _extra() == {"app": "myapp"}
"""


def _pytest(tmp_path: Path, *, with_root_conftest: bool) -> subprocess.CompletedProcess[str]:
    """Run the leaking test and the reading test in a pytest rooted in `tmp_path`.

    The repository's root `conftest.py` is loaded as a plugin from the repository root, or not.
    """
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_pair.py").write_text(TESTS)
    (tmp_path / "conftest.py").write_text(HOST)
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    plugin = ["-p", "conftest"] if with_root_conftest else []
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path),
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-q",
            *plugin,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_with_the_root_conftest_the_next_test_reads_what_the_host_set(tmp_path):
    result = _pytest(tmp_path, with_root_conftest=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_CONTROL_without_it_the_reader_fails_on_the_leak(tmp_path):
    result = _pytest(tmp_path, with_root_conftest=False)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 failed, 1 passed" in result.stdout
    assert "test_2_the_next_test_reads_what_the_host_set" in result.stdout
    assert "'service': 'leaker'" in result.stdout


def test_the_root_conftest_is_where_the_fixture_lives():
    assert f"def {FIXTURE}" in (ROOT / "conftest.py").read_text()
