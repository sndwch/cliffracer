"""A crash under `cliffracer run` prints the frames of its traceback and not the values in them.

loguru's default sink has `diagnose=True`: it prints the value of every expression on each line of
a traceback. A service that crashes while it holds a credential in a local variable, or in the
config overlay `--config` laid over it, then printed that credential, about 230 lines of values for
one startup crash. `--log-level` installs a sink without it, but a run without the flag kept the
default one. `cliffracer run` replaces the default sink with one that has `diagnose=False` at the
level the default sink had, and leaves a sink the host already replaced alone. These run the real
command in a child process, because the sink belongs to the process.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.unit.cli_fixtures.crashes_with_a_local_secret import PASSWORD, Crashes

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
TARGET = f"{Crashes.__module__}:{Crashes.__name__}"


def _run(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cliffracer.cli", "run", TARGET, *argv],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(env or {})},
    )


@pytest.mark.parametrize("flags", [(), ("--log-level", "DEBUG"), ("--log-level", "ERROR")])
def test_a_startup_crash_prints_no_value_held_by_its_frames(flags):
    done = _run(*flags)

    assert done.returncode != 0, done.stderr
    assert "Service crashed" in done.stderr, done.stderr
    assert "RuntimeError" in done.stderr and "start" in done.stderr
    assert PASSWORD not in done.stderr, "a frame value reached stderr"


def test_CONTROL_loguru_default_sink_does_print_a_frame_value(tmp_path):
    """The instrument can fail: a process that keeps the default sink prints the value.

    The script runs from a file: loguru prints frame values from the source it reads, and on
    Python 3.12 it cannot read the source of a `-c` script."""
    script = textwrap.dedent(
        f"""
        from loguru import logger

        def fails():
            password = {PASSWORD!r}
            raise RuntimeError(len(password))

        try:
            fails()
        except RuntimeError:
            logger.exception("crashed")
        """
    )

    path = tmp_path / "crash.py"
    path.write_text(script)

    done = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=60)

    assert PASSWORD in done.stderr, done.stderr


def test_the_default_level_is_kept_without_the_flag():
    done = _run()

    assert "Starting runner" in done.stderr, "INFO lines still reach stderr: " + done.stderr


def test_CONTROL_a_level_given_still_filters():
    done = _run("--log-level", "ERROR")

    assert "Starting runner" not in done.stderr, done.stderr
    assert "Service crashed" in done.stderr


def test_the_level_loguru_was_given_by_its_environment_is_kept_without_the_flag():
    done = _run(env={"LOGURU_LEVEL": "ERROR"})

    assert "Starting runner" not in done.stderr, done.stderr
    assert "Service crashed" in done.stderr
    assert PASSWORD not in done.stderr
