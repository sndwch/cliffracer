"""The examples gate's two inputs are what the runner really does, not what a test wrote down.

`tests/integration/test_examples_run.py` starts each example in a subprocess through `_BOOTSTRAP`
and decides whether it crashed by scanning its output for the line the orchestrator logs. Each of
those was read from a stand-in: the bootstrap left the test runner's `sys.argv` in the example, and
the crash reader was only ever shown text its own tests wrote. Both are broker-free to check.
"""

import asyncio
import json
import subprocess
import sys

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, ServiceRunner
from tests.integration.test_examples_run import _BOOTSTRAP, crashed_services

pytestmark = pytest.mark.unit


def test_an_example_sees_the_argv_it_documents(tmp_path):
    """The bootstrap runs the example as `python example.py`, so its argv is just the path.

    Left alone, the example's `sys.argv` was `['-c', '<broker url or "">', '<path>']`, and an
    example that dispatches on argv (`examples/basic/simple_service.py` does) printed its usage
    string and exited 0: reported as started, no service ever built.
    """
    example = tmp_path / "echo_argv.py"
    example.write_text("import json, sys\nprint('ARGV=' + json.dumps(sys.argv))\n")

    done = subprocess.run(
        [sys.executable, "-c", _BOOTSTRAP, "", str(example)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr[-1500:]
    (reported,) = [line for line in done.stdout.splitlines() if line.startswith("ARGV=")]
    assert json.loads(reported.removeprefix("ARGV=")) == [str(example)], reported


async def test_the_crash_reader_finds_the_line_the_runner_really_logs():
    """Read from the orchestrator's own log, not a paraphrase of it.

    The orchestrator catches every service exception and keeps the process alive, so for an
    orchestrated example this line is the only thing between a broken example and a green test.
    A runner whose service fails to construct logs it; the reader must report it.
    """

    class Crashes(CliffracerService):
        def __init__(self, config: ServiceConfig | None = None, **kwargs):
            raise RuntimeError("boom in the constructor")

    runner = ServiceRunner(Crashes, ServiceConfig(name="crashy", auto_restart=False, health_port=0))
    runner._running = True
    # the restart backoff waits on this event, so a set one returns at once
    runner._shutdown_event.set()
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="ERROR")
    try:
        await asyncio.wait_for(runner._run_service(), timeout=10)
    finally:
        logger.remove(sink)

    assert crashed_services("".join(lines)) == ["Service crashed: boom in the constructor"], lines
