"""The heartbeat vouches for a slow handler; it must not vouch for a wedged one.

`_JetStreamHeartbeat` pulses `in_progress()` every `ack_wait / 2` for as long as
the handler has not returned, which is right for a handler that is legitimately
slow -- the README's "long enough that a slow handler is not redelivered under
itself". For a handler that never returns it is a trap: the ack timer is reset
forever, so `num_delivered` never increments, so the `num_delivered >=
jetstream_max_deliver` branch is unreachable, so nothing is ever terminated or
dead-lettered. The poison-message safety net is disabled by exactly the failure
it exists to catch, and the replica holds the stream slot with no alarm beyond
the message never completing.

`max_processing_time` bounds it. It is **None by default**: every existing
deployment keeps today's behaviour, and a default that started cancelling
handlers would be a change nobody asked for.

CANCELLATION IS THE HALF THAT MATTERS. Ending the heartbeat alone would let the
server redeliver while the wedged coroutine ran on, so a wedged replica would
accumulate one leaked task per delivery -- a stuck message plus a leak, which is
worse than the stuck message. `wait_for` cancels the handler, and the tests
below assert the task is gone by comparing the task SET rather than counting,
because a count matches if one task dies and another is born.

Cancelling mid-flight is acceptable here and is not a general licence: a
JetStream handler already lives under redelivery, so partial work followed by a
retry is the contract it signed. A handler that cannot tolerate cancellation
cannot tolerate redelivery either.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

WEDGED = asyncio.Event  # never set, so `await WEDGED().wait()` never returns


class _Svc(CliffracerService):
    @listener("events.ping", durable="pinger")
    async def on_ping(self, subject: str, seq: int = 0):
        self.entered.append(seq)
        try:
            if self.work is None:
                await WEDGED().wait()
            else:
                await asyncio.sleep(self.work)
            self.finished.append(seq)
        finally:
            self.left.append(seq)


def _config(budget, ack_wait=0.1, max_deliver=2):
    return ServiceConfig(
        name="pinger",
        jetstream_enabled=True,
        jetstream_ack_wait=ack_wait,
        jetstream_max_deliver=max_deliver,
        max_processing_time=budget,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )


async def _deliver(*, budget, work, num_delivered=1, wait=2.0):
    """Drive one delivery and report what happened to the message AND the task.

    `work=None` wedges the handler. The task set is captured either side so a
    leaked handler task is visible as an identity, not as an arithmetic
    coincidence.
    """
    svc = _Svc(_config(budget))
    svc.entered, svc.finished, svc.left, svc.work = [], [], [], work
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    svc._discover_handlers()

    msg = AsyncMock()
    msg.subject, msg.data, msg.headers = "events.ping", b'{"seq": 1}', None
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)

    before = asyncio.all_tasks()
    dispatch = asyncio.create_task(
        svc.container._handle_jetstream_event(msg, pattern="events.ping")
    )
    done, _ = await asyncio.wait({dispatch}, timeout=wait)
    if not done:
        dispatch.cancel()
        try:
            await dispatch
        except asyncio.CancelledError:
            pass
    await asyncio.sleep(0.05)  # let a cancelled handler finish unwinding

    leaked = {t for t in asyncio.all_tasks() - before if not t.done() and t is not dispatch}
    return SimpleNamespace(
        returned=bool(done),
        disposition=[
            n for n, c in (("ack", msg.ack), ("nak", msg.nak), ("term", msg.term)) if c.await_count
        ],
        dlq=[
            c
            for rec in (svc.nc, svc.js)
            for c in rec.publish.await_args_list
            if c.args and str(c.args[0]).startswith("dlq.")
        ],
        pulses=msg.in_progress.await_count,
        entered=svc.entered,
        left=svc.left,
        finished=svc.finished,
        leaked=leaked,
    )


async def test_a_wedged_handler_is_cancelled_and_the_message_redelivered():
    out = await _deliver(budget=0.3, work=None)

    assert out.returned, "dispatch never returned; the budget did not fire"
    assert out.disposition == ["nak"], out.disposition
    assert out.entered == [1], "the handler must actually have started"
    assert out.left == [1], "the handler was abandoned rather than cancelled"
    assert out.finished == [], "a wedged handler cannot have finished"


async def test_a_cancelled_handler_leaves_no_task_behind():
    """The half that ending the heartbeat alone would not give.

    By set, not by count: a count is satisfied if one task dies and another is
    created in the same window.
    """
    out = await _deliver(budget=0.3, work=None)

    assert out.leaked == set(), [t.get_name() for t in out.leaked]


async def test_a_wedged_handler_is_dead_lettered_once_max_deliver_is_spent():
    """The branch that was unreachable: redeliveries have to end somewhere."""
    out = await _deliver(budget=0.3, work=None, num_delivered=2)

    assert out.disposition == ["term"], out.disposition
    assert len(out.dlq) == 1, f"expected one dead-letter record, got {out.dlq}"


async def test_CONTROL_a_slow_handler_under_the_budget_is_still_vouched_for():
    """The property the heartbeat exists for, which a bound could easily break.

    0.25s of work against a 0.1s `ack_wait` is two and a half ack periods: with
    no heartbeat this would be redelivered under itself.
    """
    out = await _deliver(budget=1.0, work=0.25)

    assert out.disposition == ["ack"], out.disposition
    assert out.finished == [1], "a handler inside its budget must run to completion"
    assert out.pulses >= 2, f"the heartbeat must still pulse for a slow handler: {out.pulses}"


async def test_CONTROL_the_default_leaves_todays_behaviour_alone():
    """`None` is the default, and it must mean exactly what it meant before.

    A wedged handler is still heartbeated indefinitely and still gets no
    disposition. That is the defect, and it stays until an operator opts in --
    which is why this test asserts the bug rather than the fix.
    """
    out = await _deliver(budget=None, work=None, wait=0.6)

    assert not out.returned, "with no budget, dispatch must still be waiting"
    assert out.disposition == [], out.disposition
    assert out.pulses >= 3, f"the heartbeat must still be pulsing: {out.pulses}"


async def test_CONTROL_a_handler_that_raises_is_unaffected_by_the_budget():
    """A budget must not change how an ordinary failure is dispositioned."""

    class Boom(CliffracerService):
        @listener("events.ping", durable="pinger")
        async def on_ping(self, subject: str, seq: int = 0):
            raise RuntimeError("handler exploded")

    svc = Boom(_config(1.0))
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    svc._discover_handlers()
    msg = AsyncMock()
    msg.subject, msg.data, msg.headers = "events.ping", b'{"seq": 1}', None
    msg.metadata = SimpleNamespace(num_delivered=1)

    await svc.container._handle_jetstream_event(msg, pattern="events.ping")

    assert msg.nak.await_count == 1
    assert msg.ack.await_count == 0


async def test_the_warning_names_the_handler_the_budget_and_the_elapsed_time(caplog):
    """An operator reading this has to pick a number and find the code.

    The subject alone does not name a function when the listener is registered
    against a wildcard, so the handler's own name goes in the line.
    """
    import logging

    from loguru import logger

    handler_id = logger.add(caplog.handler, level="WARNING", format="{message}")
    try:
        with caplog.at_level(logging.WARNING):
            await _deliver(budget=0.2, work=None)
    finally:
        logger.remove(handler_id)

    assert "on_ping" in caplog.text, caplog.text
    assert "max_processing_time" in caplog.text, caplog.text
    assert "0.2" in caplog.text, caplog.text
    assert "cancelled after" in caplog.text, caplog.text
