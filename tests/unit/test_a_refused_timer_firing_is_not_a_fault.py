"""A timer firing an extension turns away is a refusal, not a fault.

Everywhere else a refusal is the caller being turned away and a fault is the service being broken,
and the two go to different people. A refused firing used to be logged at ERROR with a traceback
and counted in `error_count`, so an auth hook that does its job read as a failing timer.
"""

from __future__ import annotations

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, timer
from cliffracer.core.extension import Extension, RejectMessage, RetryMessage

pytestmark = pytest.mark.unit


class Gate(Extension):
    """Refuses a firing when the service says to, with the reason the service gives."""

    async def worker_setup(self, ctx):
        refusal = getattr(self.service, "refuse_next", None)
        if refusal is not None:
            self.service.refuse_next = None
            raise refusal


class Svc(CliffracerService):
    gate = Gate()

    def __init__(self, config):
        super().__init__(config)
        self.refuse_next: RejectMessage | None = None
        self.fail_next = False
        self.ran = 0

    @timer(interval=0.01)
    async def tick(self):
        self.ran += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("the method broke")


async def _fire(svc: Svc, timer_) -> list[tuple[str, str, bool]]:
    lines: list[tuple[str, str, bool]] = []
    sink = logger.add(
        lambda m: lines.append(
            (m.record["level"].name, m.record["message"], m.record["exception"] is not None)
        ),
        level="DEBUG",
    )
    try:
        await timer_._execute_method()
    finally:
        logger.remove(sink)
    return lines


async def _service():
    svc = Svc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    t = svc._timers[0]
    t.service_instance = svc
    t.method_name = "tick"
    return svc, t


@pytest.mark.asyncio
async def test_a_refused_firing_is_a_warning_with_no_traceback_and_no_error():
    svc, t = await _service()
    svc.refuse_next = RejectMessage("not today")

    lines = await _fire(svc, t)

    assert svc.ran == 0
    assert [(lvl, msg, tb) for lvl, msg, tb in lines if lvl in ("WARNING", "ERROR")] == [
        ("WARNING", "Timer method tick refused: not today", False)
    ]
    assert (t.error_count, t.refusal_count, t.execution_count) == (0, 1, 0)
    assert (t.last_error, t.last_refusal) == (None, "not today")


@pytest.mark.asyncio
async def test_a_retry_refusal_is_a_refusal_too():
    svc, t = await _service()
    svc.refuse_next = RetryMessage("later", retry_after=5)

    await _fire(svc, t)

    assert (t.error_count, t.refusal_count, t.last_refusal) == (0, 1, "later")


@pytest.mark.asyncio
async def test_a_method_that_raises_is_still_an_error_logged_with_its_traceback():
    svc, t = await _service()
    svc.fail_next = True

    lines = await _fire(svc, t)

    errors = [(msg, tb) for lvl, msg, tb in lines if lvl == "ERROR"]
    assert errors == [("Error executing timer method tick: the method broke", True)]
    assert (t.error_count, t.refusal_count, t.execution_count) == (1, 0, 1)
    assert (t.last_error, t.last_refusal) == ("RuntimeError: the method broke", None)


@pytest.mark.asyncio
async def test_a_refusal_neither_moves_the_error_rate_nor_the_average_duration():
    svc, t = await _service()

    await _fire(svc, t)  # ok
    svc.refuse_next = RejectMessage("no")
    await _fire(svc, t)  # refused
    svc.fail_next = True
    await _fire(svc, t)  # fault
    svc.refuse_next = RejectMessage("no")
    await _fire(svc, t)  # refused

    stats = t.get_stats()
    assert (stats["execution_count"], stats["error_count"], stats["refusal_count"]) == (2, 1, 2)
    assert stats["error_rate"] == 50.0
    assert stats["average_execution_time"] == t.total_execution_time / 2


@pytest.mark.asyncio
async def test_the_next_firing_that_runs_clears_the_last_refusal():
    svc, t = await _service()
    svc.refuse_next = RejectMessage("no")
    await _fire(svc, t)
    assert t.last_refusal == "no"

    await _fire(svc, t)

    assert svc.ran == 1
    assert t.last_refusal is None and t.refusal_count == 1
