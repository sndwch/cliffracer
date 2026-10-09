"""A cron handler that stops its own service finishes the stop instead of waiting on itself.

`CronTimer` inherits `Timer.stop`, which waited for the task it was running in and then cancelled
it, a cycle that ended in `RecursionError` with the stop never returning. See the matching test for
`@timer` in `tests/unit`.
"""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from cliffracer_cron import cron

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core import dial
from cliffracer.testing import FakeClock

pytestmark = pytest.mark.unit


@pytest.fixture
def broker(monkeypatch):
    made: list[AsyncMock] = []

    async def connect(*_args, **_kwargs):
        nc = AsyncMock()
        nc.is_connected = True
        nc.is_closed = nc.is_draining = nc.is_connecting = nc.is_reconnecting = False

        async def subscribe(*_a, **_k):
            return AsyncMock()

        async def close():
            nc.is_closed, nc.is_connected = True, False

        nc.subscribe, nc.close = subscribe, close
        made.append(nc)
        return nc

    monkeypatch.setattr(dial, "connect", connect)
    return made


class Retiring(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="retiring", health_port=0, shutdown_timeout=1.0))
        self.finished = asyncio.Event()
        self.shutdown_ran = False

    async def on_shutdown(self):
        self.shutdown_ran = True

    @cron("* * * * * *")
    async def watchdog(self):
        await self.stop()
        self.finished.set()


async def test_a_cron_handler_that_stops_its_service_returns_and_the_service_is_stopped(
    broker, monkeypatch
):
    seen: list[BaseException | None] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: seen.append(context.get("exception"))
    )
    clock = FakeClock()  # on a whole second, so the next occurrence is one second away
    for declared in Retiring.watchdog._cliffracer_timers:
        monkeypatch.setattr(declared, "clock", clock)
    svc = Retiring()
    await svc.start()
    (instance,) = svc.container.registry.timers
    clock.watch(instance.task)

    started = time.monotonic()
    await clock.advance(1.0)
    await asyncio.wait_for(svc.finished.wait(), timeout=8)

    # Upper bound: 1.0 s is the service's stop grace, `shutdown_timeout=1.0` in `Retiring.__init__`
    # above. A stop that waited on its own run would spend all of it before cancelling the run.
    assert time.monotonic() - started < 1.0
    assert svc.container.is_stopped and svc.shutdown_ran
    assert broker[0].is_closed
    await clock.advance(3.0)  # three more occurrences: a cron still running would fire in each
    assert not instance.is_running and instance.task is not None and instance.task.done()
    assert instance.execution_count == 1
    assert seen == []
