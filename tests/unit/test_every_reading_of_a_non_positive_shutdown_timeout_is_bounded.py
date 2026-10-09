"""Each place that hands `shutdown_timeout` on reads a value at or below zero as the ceiling.

`ServiceConfig` refuses such a value, so it reaches these places only around validation. Read as it
was, it was a wait of no time: the connection's drain was skipped, the timers' runs were cancelled
at once, and the runner, the service host and the metrics pool were given a bound of nothing. Each
now reads it as `ON_SHUTDOWN_CEILING` and logs a warning naming it, as the stop's own steps do. The
ceiling is patched small, so a bounded wait is told from none in a test's time.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cliffracer_metrics import PoolExtension
from loguru import logger

import cliffracer.core.service as service_module
from cliffracer import CliffracerService, ServiceConfig, timer
from cliffracer.core import lifecycle
from cliffracer.core.connection import ConnectionManager
from cliffracer.core.extension import ExtensionSetupContext
from cliffracer.runners.orchestrator import ServiceRunner

pytestmark = pytest.mark.unit

CEILING = 0.3
VALUES = pytest.mark.parametrize("shutdown_timeout", [0, -1], ids=["zero", "negative"])


@pytest.fixture(autouse=True)
def small_ceiling(monkeypatch):
    monkeypatch.setattr(lifecycle, "ON_SHUTDOWN_CEILING", CEILING)


@pytest.fixture
def warnings_said():
    said: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: said.append((m.record["level"].name, m.record["message"])), level="WARNING"
    )
    yield said
    logger.remove(sink)


def _warning(where: str, value: float) -> tuple[str, str]:
    return (
        "WARNING",
        f"{where}: shutdown_timeout is {value!r}, which is not a duration; bounding it at "
        f"{CEILING:g} seconds (ON_SHUTDOWN_CEILING). Use None to wait without a deadline",
    )


def _config(shutdown_timeout, **fields) -> ServiceConfig:
    config = ServiceConfig(name="svc", health_port=0, **fields)
    object.__setattr__(config, "shutdown_timeout", shutdown_timeout)  # around validation
    return config


@VALUES
async def test_the_connection_drain_is_given_the_ceiling(shutdown_timeout, warnings_said):
    connection = ConnectionManager(_config(shutdown_timeout))
    nc = MagicMock()
    nc.is_connected, nc.is_closed, nc.is_draining = True, False, False
    nc.is_connecting = nc.is_reconnecting = False
    drained = asyncio.Event()

    async def slow_drain() -> None:
        await asyncio.sleep(CEILING * 0.5)
        drained.set()

    nc.drain = slow_drain
    nc.close = AsyncMock()
    connection.nc = nc

    await asyncio.wait_for(connection.disconnect(), timeout=5)

    assert drained.is_set(), "the drain was cut off before the ceiling"
    assert _warning("Draining the broker connection", shutdown_timeout) in warnings_said


class OneRun(CliffracerService):
    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.running = asyncio.Event()
        self.finished = False

    @timer(interval=30.0, eager=True)
    async def tick(self) -> None:
        self.running.set()
        await asyncio.sleep(CEILING * 0.5)
        self.finished = True


@VALUES
async def test_the_timers_run_in_flight_is_given_the_ceiling(shutdown_timeout, warnings_said):
    service = OneRun(_config(shutdown_timeout))
    service._discover_handlers()
    for each in service.container.registry.timers:
        await each.start(service)
    await asyncio.wait_for(service.running.wait(), 2)

    await asyncio.wait_for(service.container._stop_timers(), timeout=5)

    assert service.finished, "the run in flight was cancelled instead of given the ceiling"
    assert _warning("Stopping the timers", shutdown_timeout) in warnings_said


class Quiet:
    def __init__(self, config: ServiceConfig) -> None:
        self.config = config


@VALUES
def test_the_runners_bound_on_a_stop_after_a_cancel_is_the_ceiling(shutdown_timeout, warnings_said):
    runner = ServiceRunner(Quiet, config=_config(shutdown_timeout))

    assert runner._teardown_timeout() == CEILING
    assert (
        _warning("The runner's bound on a stop after a cancel", shutdown_timeout) in warnings_said
    )


@VALUES
def test_the_service_hosts_teardown_bound_is_the_ceiling(
    shutdown_timeout, warnings_said, monkeypatch
):
    handed: dict = {}

    def capture(main, *, teardown_timeout):
        main.close()
        handed["bound"] = teardown_timeout

    monkeypatch.setattr(service_module, "run_hosted", capture)
    CliffracerService(_config(shutdown_timeout)).run()

    assert handed["bound"]() == CEILING
    assert _warning("Joining the tasks left at teardown", shutdown_timeout) in warnings_said


@VALUES
async def test_the_metrics_pools_drain_is_the_ceiling(shutdown_timeout, warnings_said):
    extension = PoolExtension(max_connections=1)
    context = ExtensionSetupContext(
        service_config=_config(shutdown_timeout),
        broker_url="nats://broker.invalid:4222",
        service=SimpleNamespace(),
    )

    await extension.setup(context)

    assert extension.pool is not None
    assert extension.pool.drain_timeout == CEILING
    assert _warning("Draining the connection pool", shutdown_timeout) in warnings_said


# --- `None` is still no deadline at every place ---------------------------------------------------
# Each wait below outlasts the ceiling, so a `None` read as the ceiling, or as zero, cuts it off.


async def _none_at_the_connection_drain(monkeypatch) -> bool:
    connection = ConnectionManager(_config(None))
    nc = MagicMock()
    nc.is_connected, nc.is_closed, nc.is_draining = True, False, False
    nc.is_connecting = nc.is_reconnecting = False
    drained = asyncio.Event()

    async def slow_drain() -> None:
        await asyncio.sleep(CEILING * 2)
        drained.set()

    nc.drain = slow_drain
    nc.close = AsyncMock()
    connection.nc = nc
    await asyncio.wait_for(connection.disconnect(), timeout=5)
    return drained.is_set()


class LongRun(OneRun):
    @timer(interval=30.0, eager=True)
    async def tick(self) -> None:
        self.running.set()
        await asyncio.sleep(CEILING * 2)
        self.finished = True


async def _none_at_the_timers(monkeypatch) -> bool:
    service = LongRun(_config(None))
    service._discover_handlers()
    for each in service.container.registry.timers:
        await each.start(service)
    await asyncio.wait_for(service.running.wait(), 2)
    await asyncio.wait_for(service.container._stop_timers(), timeout=5)
    return service.finished


async def _none_at_the_runner(monkeypatch) -> bool:
    return ServiceRunner(Quiet, config=_config(None))._teardown_timeout() is None


async def _none_at_the_service_host(monkeypatch) -> bool:
    handed: dict = {}

    def capture(main, *, teardown_timeout):
        main.close()
        handed["bound"] = teardown_timeout

    monkeypatch.setattr(service_module, "run_hosted", capture)
    CliffracerService(_config(None)).run()
    return handed["bound"]() is None


async def _none_at_the_metrics_pool(monkeypatch) -> bool:
    extension = PoolExtension(max_connections=1)
    await extension.setup(
        ExtensionSetupContext(
            service_config=_config(None),
            broker_url="nats://broker.invalid:4222",
            service=SimpleNamespace(),
        )
    )
    return extension.pool is not None and extension.pool.drain_timeout is None


@pytest.mark.parametrize(
    "keeps_no_deadline",
    [
        _none_at_the_connection_drain,
        _none_at_the_timers,
        _none_at_the_runner,
        _none_at_the_service_host,
        _none_at_the_metrics_pool,
    ],
    ids=["connection-drain", "timers", "runner", "service-host", "metrics-pool"],
)
async def test_CONTROL_none_is_still_no_deadline_at_each_place(
    keeps_no_deadline, warnings_said, monkeypatch
):
    assert await keeps_no_deadline(monkeypatch), "None was read as a deadline"
    assert warnings_said == []
