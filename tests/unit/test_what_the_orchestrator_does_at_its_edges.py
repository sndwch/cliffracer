"""ServiceRunner and ServiceOrchestrator at their edges: signals, log lines, cut-off starts, restarts, grace."""

import asyncio
import json
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService
from cliffracer.core import ServiceConfig
from cliffracer.introspect import describe
from cliffracer.runners import orchestrator
from cliffracer.runners.orchestrator import (
    RUNNER_OK,
    RUNNER_SERVICE_DOWN,
    ServiceOrchestrator,
    ServiceRunner,
    _set_from_signal,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def records():
    captured = []
    sink = logger.add(
        lambda message: captured.append(message.record), level="DEBUG", format="{message}"
    )
    yield captured
    logger.remove(sink)


def errors(records, text):
    return [r for r in records if r["level"].name in {"ERROR", "CRITICAL"} and text in r["message"]]


@pytest.fixture
def handlers(monkeypatch):
    installed = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.__setitem__(sig, handler))
    return installed


# --- signals --------------------------------------------------------------------------------------


def test_a_signal_before_any_loop_sets_the_event():
    event = asyncio.Event()
    _set_from_signal(event, None)
    assert event.is_set()


def test_a_signal_after_the_loop_closed_sets_the_event():
    loop = asyncio.new_event_loop()
    loop.close()
    event = asyncio.Event()
    _set_from_signal(event, loop)
    assert event.is_set()


@pytest.mark.parametrize("make", [lambda: ServiceRunner(object), ServiceOrchestrator])
def test_the_entry_points_take_sigterm_and_sigint_and_a_signal_before_run_stops(handlers, make):
    owner = make()
    owner._setup_signal_handlers()
    assert {signal.SIGTERM, signal.SIGINT} <= set(handlers)
    handlers[signal.SIGINT](signal.SIGINT, None)
    assert owner._shutdown_event.is_set()


@pytest.mark.parametrize("make", [lambda: ServiceRunner(object), ServiceOrchestrator])
def test_on_windows_the_entry_points_also_take_sigbreak(handlers, monkeypatch, make):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
    make()._setup_signal_handlers()
    assert 21 in handlers


# --- the runner's log lines -----------------------------------------------------------------------


class Orders:
    """Self-configuring, crashes on start, not restarted."""

    stops = 0

    def __init__(self):
        self.config = ServiceConfig(name="orders", health_port=0, auto_restart=False)

    async def start(self):
        raise RuntimeError("boom")

    async def stop(self):
        type(self).stops += 1


async def test_lines_name_the_service_from_its_own_config_once_it_exists(records):
    assert await ServiceRunner(Orders).run() == RUNNER_SERVICE_DOWN
    after = [r for r in records if "attempt #1" in r["message"]]
    assert after and after[0]["extra"].get("service") == "orders"


async def test_before_a_service_exists_lines_name_the_runners_config_or_nothing(records):
    await ServiceRunner(Orders).run()
    first = [r for r in records if "Starting runner" in r["message"]]
    assert first and "service" not in first[0]["extra"]
    records.clear()

    class Named:
        def __init__(self, config):
            self.config = config

        async def start(self):
            raise RuntimeError("boom")

        async def stop(self):
            pass

    await ServiceRunner(Named, config=ServiceConfig(name="billing", auto_restart=False)).run()
    first = [r for r in records if "Starting runner" in r["message"]]
    assert first and first[0]["extra"].get("service") == "billing"


async def test_a_constructor_the_runner_cannot_call_is_logged_as_an_error(records):
    class Unconstructable:
        def __init__(self, config, required_extra):
            pass

    assert await ServiceRunner(Unconstructable).run() == RUNNER_SERVICE_DOWN
    assert errors(records, "Cannot construct Unconstructable")


async def test_a_permanently_down_runner_says_so_naming_its_service(records):
    class NoArgs:
        def __init__(self):
            raise TypeError("cannot")

    status = await ServiceRunner(NoArgs, config=ServiceConfig(name="ledger")).run()
    assert status == RUNNER_SERVICE_DOWN
    assert errors(
        records, "Runner for service 'ledger' stopped because the service is permanently down"
    )


async def test_a_runner_whose_loop_raises_logs_the_failure_and_reports_down(records, monkeypatch):
    runner = ServiceRunner(Orders)

    async def broken():
        raise RuntimeError("loop broke")

    monkeypatch.setattr(runner, "_run_service", broken)
    assert await runner.run() == RUNNER_SERVICE_DOWN
    assert errors(records, "Runner for service 'Orders' failed")


# --- start, cut-offs and cancellation -------------------------------------------------------------


class Recorder:
    events: list[str] = []
    start_gate: asyncio.Event | None = None
    unwind = 0.2
    raise_on_cancel = False

    def __init__(self):
        self.config = ServiceConfig(
            name="recorder", health_port=0, auto_restart=False, shutdown_timeout=1
        )

    async def start(self):
        type(self).events.append("start")
        self.starting = asyncio.current_task()
        try:
            await type(self).start_gate.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(type(self).unwind)
            type(self).events.append("start unwound")
            if type(self).raise_on_cancel:
                raise RuntimeError("start turned the cancel into another error") from None
            raise

    async def stop(self):
        """Like a lifecycle's stop: cancel the task that is starting the service."""
        type(self).events.append("stop")
        starting = getattr(self, "starting", None)
        if starting is not None and not starting.done():
            starting.cancel()


@pytest.fixture
def recorder():
    Recorder.events = []
    Recorder.start_gate = asyncio.Event()
    Recorder.raise_on_cancel = False
    yield Recorder
    Recorder.start_gate.set()


async def test_a_shutdown_during_start_stops_once_and_waits_for_the_start(recorder):
    host = ServiceOrchestrator()
    host.add_service(Recorder)
    running = asyncio.create_task(host.run())
    while "start" not in recorder.events:
        await asyncio.sleep(0)
    await host.stop()
    assert await running == RUNNER_OK
    assert recorder.events == ["start", "stop", "start unwound"], recorder.events


@pytest.mark.parametrize("raise_on_cancel", [False, True])
async def test_cancelling_run_mid_start_waits_for_the_start_then_stops_and_raises(
    recorder, raise_on_cancel
):
    recorder.raise_on_cancel = raise_on_cancel
    runner = ServiceRunner(Recorder)
    running = asyncio.create_task(runner.run())
    while "start" not in recorder.events:
        await asyncio.sleep(0)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert recorder.events == ["start", "start unwound", "stop"], recorder.events


class CountsStops:
    stops = 0

    def __init__(self):
        self.config = ServiceConfig(name="counts", health_port=0, auto_restart=False)

    async def start(self):
        raise RuntimeError("boom")

    async def stop(self):
        type(self).stops += 1


async def test_a_service_whose_start_raised_is_stopped():
    CountsStops.stops = 0
    assert await ServiceRunner(CountsStops).run() == RUNNER_SERVICE_DOWN
    assert CountsStops.stops == 1


class SlowOrFailingStop:
    mode = "slow"

    def __init__(self):
        self.config = ServiceConfig(name="stuck", health_port=0, shutdown_timeout=0.05)

    async def start(self):
        pass

    async def stop(self):
        if type(self).mode == "slow":
            await asyncio.sleep(5)
        raise RuntimeError("stop failed")


@pytest.mark.parametrize(
    "mode, line",
    [("slow", "did not stop within its shutdown_timeout"), ("fails", "after a cancel failed")],
)
async def test_a_stop_after_cancel_that_overruns_or_fails_is_an_error(records, mode, line):
    SlowOrFailingStop.mode = mode
    runner = ServiceRunner(SlowOrFailingStop)
    running = asyncio.create_task(runner.run())
    while runner.service is None:
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert errors(records, line)


# --- restart decisions ----------------------------------------------------------------------------


class AlwaysCrashes:
    starts = 0

    def __init__(self):
        self.config = ServiceConfig(name="flapper", health_port=0, restart_delay=100)

    async def start(self):
        type(self).starts += 1
        raise RuntimeError("start fails")

    async def stop(self):
        pass


async def test_a_restart_delay_above_the_cap_is_kept_on_every_wait(monkeypatch):
    AlwaysCrashes.starts = 0
    runner = ServiceRunner(AlwaysCrashes)
    waits = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(awaitable, timeout):
        if len(waits) < 3:
            waits.append(timeout)
            awaitable.close()
            raise TimeoutError
        runner._running = False
        runner._shutdown_event.set()
        return await real_wait_for(awaitable, timeout=None)

    monkeypatch.setattr(orchestrator.asyncio, "wait_for", recording_wait_for)
    await runner.run()
    assert waits == [100, 100, 100]


def _broker(monkeypatch, cls):
    async def connect(*args, **kwargs):
        nc = AsyncMock()
        nc.is_connected = True
        nc.is_closed = nc.is_draining = nc.is_connecting = nc.is_reconnecting = False
        nc.request.return_value = SimpleNamespace(data=json.dumps(describe(cls).to_dict()).encode())

        async def close():
            nc.is_closed = True

        nc.close = close
        return nc

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)


class FailsOnStartup(CliffracerService):
    attempts = 0
    resist = False

    def __init__(self):
        super().__init__(
            ServiceConfig(
                name="fails_on_startup", health_port=0, restart_delay=0.01, shutdown_timeout=0.05
            )
        )

    async def on_startup(self):
        type(self).attempts += 1
        if type(self).resist:
            self.container.lifecycle.spawn_supervised_task(self._resist(), name="resists-cancel")
            await asyncio.sleep(0)
        raise RuntimeError("startup fails")

    async def _resist(self):
        for _ in range(40):
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass


async def test_a_clean_restart_reports_no_unfinished_shutdown(monkeypatch, records):
    _broker(monkeypatch, FailsOnStartup)
    FailsOnStartup.attempts, FailsOnStartup.resist = 0, False
    host = ServiceOrchestrator()
    host.add_service(FailsOnStartup)
    running = asyncio.create_task(host.run())
    while FailsOnStartup.attempts < 3:
        await asyncio.sleep(0.01)
    await host.stop()
    await running
    assert not errors(records, "Cannot restart service with unfinished shutdown tasks")


async def test_a_crash_that_leaves_shutdown_work_running_is_not_restarted(monkeypatch, records):
    _broker(monkeypatch, FailsOnStartup)
    FailsOnStartup.attempts, FailsOnStartup.resist = 0, True
    runner = ServiceRunner(FailsOnStartup)
    assert await asyncio.wait_for(runner.run(), timeout=10) == RUNNER_SERVICE_DOWN
    assert FailsOnStartup.attempts == 1
    assert errors(records, "Cannot restart service with unfinished shutdown tasks")
    leftover = [t for t in asyncio.all_tasks() if t.get_name() == "resists-cancel"]
    for task in leftover:
        task.cancel()
    await asyncio.gather(*leftover, return_exceptions=True)


class FlushesOnShutdown(CliffracerService):
    """Starts; its on_shutdown spawns a short supervised flush, which runs after the drain."""

    started = 0

    def __init__(self):
        super().__init__(ServiceConfig(name="flushes", health_port=0, shutdown_timeout=0.5))

    async def on_startup(self):
        type(self).started += 1

    async def on_shutdown(self):
        self.container.lifecycle.spawn_supervised_task(asyncio.sleep(0.3), name="flush")


async def test_a_stop_with_shutdown_work_still_running_is_not_a_refused_restart(
    monkeypatch, records
):
    """Only work the drain gave up on blocks a restart: a task spawned after the drain is not it."""
    _broker(monkeypatch, FlushesOnShutdown)
    FlushesOnShutdown.started = 0
    runner = ServiceRunner(FlushesOnShutdown)
    running = asyncio.create_task(runner.run())
    try:
        async with asyncio.timeout(10):
            while FlushesOnShutdown.started == 0:
                await asyncio.sleep(0.01)
        runner._running = False
        runner._shutdown_event.set()
        assert await asyncio.wait_for(running, timeout=10) == RUNNER_OK
    finally:
        flush = [t for t in asyncio.all_tasks() if t.get_name() == "flush"]
        await asyncio.wait_for(asyncio.gather(*flush, return_exceptions=True), timeout=5)
    assert not errors(records, "unfinished shutdown tasks")


# --- teardown grace -------------------------------------------------------------------------------


class Quiet:
    def __init__(self, config=None):
        self.config = config or ServiceConfig(name="quiet", shutdown_timeout=7.0)


def test_a_runners_grace_reads_its_config_until_a_service_exists_then_the_service():
    assert ServiceRunner(Quiet)._teardown_timeout() == 30.0
    assert (
        ServiceRunner(
            Quiet, config=ServiceConfig(name="q", shutdown_timeout=3.0)
        )._teardown_timeout()
        == 3.0
    )
    runner = ServiceRunner(Quiet, config=ServiceConfig(name="q", shutdown_timeout=3.0))
    runner.service = Quiet(ServiceConfig(name="q", shutdown_timeout=0.3))
    assert runner._teardown_timeout() == 0.3


def test_an_orchestrators_grace_is_the_longest_and_none_waits_forever():
    host = ServiceOrchestrator()
    assert host._teardown_timeout() == 30.0
    for grace in (0.05, 120.0, 3.0):
        host.add_service(Quiet, config=ServiceConfig(name=f"q{grace}", shutdown_timeout=grace))
    assert host._teardown_timeout() == 120.0
    host.add_service(Quiet, config=ServiceConfig(name="forever", shutdown_timeout=None))
    assert host._teardown_timeout() is None


# --- the synchronous entry points -----------------------------------------------------------------


@pytest.mark.parametrize("make", [lambda: ServiceRunner(Orders), ServiceOrchestrator])
async def test_a_fatal_error_in_an_entry_point_exits_one_and_says_so(handlers, records, make):
    owner = make()
    with pytest.raises(SystemExit) as exited:
        owner.run_forever()  # asyncio refuses to run a loop inside this running one
    assert exited.value.code == 1
    assert errors(records, "Fatal error in")


# --- the orchestrator -----------------------------------------------------------------------------


async def test_a_runner_that_raises_is_counted_down_while_the_others_finish(monkeypatch):
    host = ServiceOrchestrator()
    host.add_service(Orders)
    host.add_service(Orders)

    async def raises():
        raise RuntimeError("runner broke")

    monkeypatch.setattr(host.runners[0], "run", raises)
    assert await host.run() == RUNNER_SERVICE_DOWN


class Steady:
    started = 0

    def __init__(self):
        self.config = ServiceConfig(name="steady", health_port=0)

    async def start(self):
        type(self).started += 1

    async def stop(self):
        pass


async def test_stopping_an_orchestrator_ends_a_service_that_started():
    Steady.started = 0
    host = ServiceOrchestrator()
    host.add_service(Steady)
    running = asyncio.create_task(host.run())
    while Steady.started == 0:
        await asyncio.sleep(0)
    await asyncio.sleep(0.1)  # into the loop that watches a started service
    await asyncio.wait_for(host.stop(), timeout=5)
    assert await running == RUNNER_OK


def test_a_runner_watching_a_started_service_gives_its_loop_back():
    """A loop that never yields cannot time itself out, so the bound is a separate process."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "tests.fixtures.runner_loop_process"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the runner never gave its event loop back while watching a started service")
    assert result.returncode == 0, result.stderr
    assert "DONE" in result.stdout
