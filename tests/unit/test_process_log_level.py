"""The `--log-level` flag, read off a log record emitted by a running service.

Loguru's sinks belong to the process, so the level does too. Each test drives
`ServiceOrchestrator.run()` -- the path `cliffracer run` takes -- and reads what
the sink wrote. Calling `_configure_logging()` directly would prove the helper
works while leaving the flag free to be wired to nothing.

`_baseline_sink` matters as much as the assertions. Loguru's own default sink
binds whatever `sys.stderr` was when loguru was first imported, which under
pytest is neither the object `capsys` reads nor the descriptor `capfd` reads --
so a test resting on it passes or fails according to what ran before it. Every
test here starts from one known permissive sink instead, and each passes run
alone as well as in file order.
"""

import asyncio
import sys
from unittest.mock import AsyncMock, patch

import pytest
from loguru import logger

from cliffracer.cli.main import build_orchestrator

pytestmark = pytest.mark.unit

MOD = "tests.unit.cli_fixtures.sample_services"

# Emitted at INFO by the orchestrator and by the connection, during run().
STARTING = "Starting "
CONNECTED = "connected to NATS"


@pytest.fixture(autouse=True)
def _baseline_sink(capsys):
    """One permissive sink on the stream `capsys` reads, restored afterwards.

    This stands in for the sink a process has before `cliffracer run` touches
    anything, so "the record was filtered" is distinguishable from "no sink was
    listening". Taking `capsys` as an argument is what orders this after pytest
    has replaced `sys.stderr`; bound before that, the sink would write somewhere
    `capsys` never reads and every assertion here would invert.
    """
    logger.remove()
    logger.add(sys.stderr, level="DEBUG")
    try:
        yield
    finally:
        logger.remove()
        logger.add(sys.__stderr__)


async def _run(level, targets=(f"{MOD}:AlphaService",)):
    """Run the orchestrator to completion, as `cliffracer run` does."""
    orchestrator = build_orchestrator(
        list(targets), nats_url=None, log_level=level, config_path=None
    )
    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    with patch("cliffracer.core.dial.connect", return_value=mock_nc):
        run_task = asyncio.create_task(orchestrator.run())
        await asyncio.sleep(0.1)
        await orchestrator.stop()
        await asyncio.wait_for(run_task, timeout=5)
    return orchestrator


async def test_nothing_a_service_logs_at_info_while_starting_reaches_the_sink(capsys):
    """The sink is configured before any service starts.

    The orchestrator's own "Starting N services" and the connection's
    "connected to NATS" are both INFO records emitted during `run()`. If the
    sink were configured after the services, those records would already have
    gone to the permissive baseline sink and be readable here.
    """
    await _run("CRITICAL")

    err = capsys.readouterr().err
    assert STARTING not in err
    assert CONNECTED not in err


async def test_the_same_startup_records_reach_the_sink_under_an_info_level(capsys):
    """The control. Without it, a sink that dropped everything -- or services
    that never started -- would look exactly like a working filter."""
    await _run("INFO")

    err = capsys.readouterr().err
    assert STARTING in err
    assert CONNECTED in err


async def test_a_record_after_the_run_is_still_filtered_at_the_process_level(capsys):
    """The level belongs to the process, so it outlives the call that set it."""
    await _run("CRITICAL")
    capsys.readouterr()

    logger.info("info record after the run")
    logger.critical("critical record after the run")

    err = capsys.readouterr().err
    assert "info record after the run" not in err
    assert "critical record after the run" in err


async def test_every_service_in_one_process_logs_at_the_one_level(capsys):
    """Two services, one sink, one level -- which is why this is a process-wide
    setting and not a per-service one."""
    orchestrator = await _run("CRITICAL", targets=(MOD,))
    assert len(orchestrator.runners) == 2, "two services in this process"

    err = capsys.readouterr().err
    assert "alpha_service" not in err
    assert "beta_service" not in err

    for name in ("alpha_service", "beta_service"):
        logger.bind(service=name).info(f"info record from {name}")
        logger.bind(service=name).critical(f"critical record from {name}")

    err = capsys.readouterr().err
    for name in ("alpha_service", "beta_service"):
        assert f"info record from {name}" not in err
        assert f"critical record from {name}" in err


async def test_without_the_flag_the_process_sink_is_left_alone(capsys):
    """`cliffracer run` with no `--log-level` must not reconfigure logging, or
    running services would change how their host application logs. The baseline
    sink is still there, still passing the INFO records the run emits."""
    await _run(None)

    err = capsys.readouterr().err
    assert STARTING in err
    assert CONNECTED in err
