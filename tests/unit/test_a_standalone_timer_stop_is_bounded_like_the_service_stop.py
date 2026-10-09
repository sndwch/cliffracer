"""A `Timer.stop()` with no service hand-over gives a cancelled run a bound, not for ever.

The service path hands a run that ignores cancellation to the drain, which bounds and reports it.
A timer used on its own (no service, or a service that stops it directly) waited for the cancelled
run with no deadline, so a callback that catches `CancelledError` held `stop()` open for as long as
it ran. `cancel_grace` is that bound: the service's `shutdown_timeout` when the timer belongs to a
service, `STANDALONE_CANCEL_GRACE` when it does not, and `None` to wait.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from loguru import logger

from cliffracer.core.timer import Timer

pytestmark = pytest.mark.unit


class Service:
    def __init__(self, shutdown_timeout=None, *, hold: float = 30.0) -> None:
        if shutdown_timeout is not None:
            self.config = SimpleNamespace(name="svc", shutdown_timeout=shutdown_timeout)
        self.hold = hold
        self.running = asyncio.Event()
        self.finished = False
        self.release = False

    async def tick(self) -> None:
        self.running.set()
        end = time.monotonic() + self.hold
        while time.monotonic() < end and not self.release:
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                pass  # a callback that ignores cancellation
        self.finished = True


async def _running_timer(service: Service) -> Timer:
    t = Timer(interval=0.05, eager=True)
    t.method_name = "tick"
    await t.start(service)
    await asyncio.wait_for(service.running.wait(), 2)
    return t


async def _let_it_finish(service: Service, t: Timer) -> None:
    """End the run the test abandoned, so no task outlives the test."""
    service.release = True
    assert t.task is not None
    await asyncio.wait_for(asyncio.shield(t.task), 2) if not t.task.done() else None


def _errors() -> tuple[list[str], int]:
    said: list[str] = []
    return said, logger.add(lambda m: said.append(m.record["message"]), level="ERROR")


async def test_a_stop_with_a_cancel_grace_returns_and_reports_the_run_that_would_not_stop():
    service = Service(hold=3.0)
    t = await _running_timer(service)
    said, sink = _errors()
    started = time.monotonic()
    try:
        await asyncio.wait_for(t.stop(cancel_grace=0.3), timeout=2)
    finally:
        logger.remove(sink)

    # Upper bound. CI p99 0.303 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.3 s,
    # 244x the overshoot; below 3 s (hold=3.0: the run waited for).
    assert time.monotonic() - started < 1.0
    assert not service.finished, "the run was waited for instead of being reported"
    assert any("did not stop within 0.3s" in line for line in said), said
    await _let_it_finish(service, t)


async def test_the_bound_defaults_to_the_services_shutdown_timeout():
    service = Service(shutdown_timeout=0.3, hold=3.0)
    t = await _running_timer(service)
    started = time.monotonic()

    await asyncio.wait_for(t.stop(), timeout=2)

    # Upper bound. CI p99 0.302 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.3 s,
    # 306x the overshoot; below 3 s (hold=3.0: the run waited for).
    assert time.monotonic() - started < 1.0
    assert not service.finished
    await _let_it_finish(service, t)


async def test_a_timer_that_belongs_to_no_service_has_the_standalone_bound():
    from cliffracer.core.timer import STANDALONE_CANCEL_GRACE

    t = Timer(interval=1.0)

    assert t._cancel_grace(Timer.stop.__kwdefaults__["cancel_grace"]) == STANDALONE_CANCEL_GRACE
    assert STANDALONE_CANCEL_GRACE == 30.0


async def test_CONTROL_a_run_that_honours_cancellation_stops_at_once():
    class Polite(Service):
        async def tick(self) -> None:
            self.running.set()
            await asyncio.sleep(30)

    service = Polite()
    t = await _running_timer(service)
    started = time.monotonic()

    await t.stop()

    # Upper bound. CI p99 0.000287 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 1743x
    # p99.
    assert time.monotonic() - started < 0.5
    assert t.task is not None and t.task.done()


async def test_CONTROL_a_cancel_grace_of_none_waits_for_the_run():
    service = Service(hold=0.6)
    t = await _running_timer(service)
    started = time.monotonic()

    await t.stop(cancel_grace=None)

    assert service.finished
    # Lower bound: the run holds 0.6 s; a stop that did not wait for it returns near 0. Load can
    # only lengthen it.
    assert time.monotonic() - started >= 0.4


async def test_a_service_shutdown_timeout_of_none_waits_for_the_run():
    """A service that waits without a bound gives its timer's cancelled run no bound either."""
    service = Service(hold=0.6)
    service.config = SimpleNamespace(name="svc", shutdown_timeout=None)
    t = await _running_timer(service)
    started = time.monotonic()

    await asyncio.wait_for(t.stop(), timeout=3)

    assert service.finished
    # Lower bound: the run holds 0.6 s; a stop that did not wait for it returns near 0. Load can
    # only lengthen it.
    assert time.monotonic() - started >= 0.4
