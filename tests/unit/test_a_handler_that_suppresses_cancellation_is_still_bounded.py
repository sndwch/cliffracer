"""`max_processing_time` must hold for a handler that refuses to be cancelled.

The budget is enforced by cancelling the handler. A handler that catches
`asyncio.CancelledError` -- or a bare `except BaseException` -- and carries on
breaks asyncio's contract, and each way of carrying on escaped the budget
differently:

- it RETURNS: the deadline's cancellation was absorbed, the await completed
  normally, and the message was ACKED as a success with nothing logged. At
  `jetstream_max_deliver` it was acked instead of dead-lettered. The work the
  deadline interrupted was lost without a trace.
- it FINISHES LATE: the same, after running past its budget for as long as it
  liked, heartbeated all the while.
- it WEDGES AGAIN: dispatch never returns, the heartbeat vouches for it
  forever, and nothing is logged. This is the defect the budget was added for.

A handler that returns after its deadline fired is now naked, as the timeout it
was. A wedged one cannot be dispositioned while it still runs -- naking under a
running handler would let the redelivery race it -- so it gets a WARNING once
it has run a second budget past the cancellation. The two warnings are worded
differently because an operator acts differently on each: one handler stopped
late, the other has not stopped.

Separately, a handler's OWN `TimeoutError` was caught by the same clause as the
deadline's and reported as a budget overrun, in the log and in the dead-letter
record, with the handler's message dropped. "The budget fired" is now decided
by the deadline, not by the exception's type.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

BUDGET = 0.2

# The distinguishing phrase of each warning. Asserted present where it belongs
# and absent where it does not, so neither can be satisfied by the other.
SUPPRESSED = "suppressed the cancellation"
STILL_RUNNING = "is still running"
OVERRUN = "exceeded max_processing_time"


class _Svc(CliffracerService):
    @listener("events.ping", durable="pinger")
    async def on_ping(self, subject: str, seq: int = 0):
        self.entered.append(seq)
        if self.shape == "own_timeout":
            await asyncio.sleep(0.01)
            raise TimeoutError("backend call timed out")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.caught.append(seq)
            if self.shape == "honour":
                raise
            if self.shape == "late":
                await asyncio.sleep(3 * BUDGET)
                self.finished.append(seq)
                return
            if self.shape == "wedge":
                await asyncio.Event().wait()
            # "return": fall through and return normally


def _config():
    return ServiceConfig(
        name="pinger",
        jetstream_enabled=True,
        jetstream_ack_wait=0.1,
        jetstream_max_deliver=2,
        max_processing_time=BUDGET,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )


def _dlq_records(svc):
    records = []
    for rec in (svc.nc, svc.js):
        for call in rec.publish.await_args_list:
            if not (call.args and str(call.args[0]).startswith("dlq.")):
                continue
            data = call.args[1] if len(call.args) > 1 else call.kwargs.get("payload")
            records.append(json.loads(data))
    return records


async def _deliver(shape, *, num_delivered=1, wait=2.0, linger=0.05):
    """Drive one delivery through the real dispatch path and report the outcome.

    Warnings are captured until `linger` has passed after dispatch ends, so a
    timer that outlives dispatch is still heard.
    """
    svc = _Svc(_config())
    svc.shape = shape
    svc.entered, svc.caught, svc.finished = [], [], []
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    svc._discover_handlers()

    msg = AsyncMock()
    msg.subject, msg.data, msg.headers = "events.ping", b'{"seq": 1}', None
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)

    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    before = asyncio.all_tasks()
    dispatch = asyncio.create_task(
        svc.container._handle_jetstream_event(msg, pattern="events.ping")
    )
    try:
        done, _ = await asyncio.wait({dispatch}, timeout=wait)
        pulses = msg.in_progress.await_count
        if not done:
            dispatch.cancel()
            try:
                await dispatch
            except asyncio.CancelledError:
                pass
        await asyncio.sleep(linger)
    finally:
        logger.remove(sink)

    leaked = {t for t in asyncio.all_tasks() - before if not t.done() and t is not dispatch}
    return SimpleNamespace(
        returned=bool(done),
        disposition=[
            n for n, c in (("ack", msg.ack), ("nak", msg.nak), ("term", msg.term)) if c.await_count
        ],
        dlq=_dlq_records(svc),
        pulses=pulses,
        caught=svc.caught,
        finished=svc.finished,
        warnings=warnings,
        leaked=leaked,
    )


def _with(warnings, phrase):
    return [w for w in warnings if phrase in w]


# --- the control: a handler that honours cancellation ------------------------


async def test_CONTROL_a_handler_that_honours_cancellation_is_naked():
    out = await _deliver("honour")

    assert out.caught == [1], "the fixture must actually reach the cancellation"
    assert out.disposition == ["nak"], out.disposition
    assert len(_with(out.warnings, OVERRUN)) == 1, out.warnings
    assert _with(out.warnings, SUPPRESSED) == [], out.warnings
    assert _with(out.warnings, STILL_RUNNING) == [], out.warnings


async def test_CONTROL_a_handler_that_honours_cancellation_is_dead_lettered_at_max_deliver():
    out = await _deliver("honour", num_delivered=2)

    assert out.disposition == ["term"], out.disposition
    assert len(out.dlq) == 1, out.dlq
    assert OVERRUN in out.dlq[0]["error"], out.dlq[0]


# --- suppresses, then returns ------------------------------------------------


async def test_a_handler_that_suppresses_cancellation_and_returns_is_naked_not_acked():
    out = await _deliver("return")

    assert out.caught == [1], "the fixture must actually reach the cancellation"
    assert out.disposition == ["nak"], out.disposition


async def test_a_handler_that_suppresses_cancellation_and_returns_is_warned_about_by_name():
    out = await _deliver("return")

    suppressed = _with(out.warnings, SUPPRESSED)
    assert len(suppressed) == 1, out.warnings
    assert "on_ping" in suppressed[0], suppressed
    assert f"max_processing_time ({BUDGET}s)" in suppressed[0], suppressed
    assert _with(out.warnings, STILL_RUNNING) == [], out.warnings


async def test_a_handler_that_suppresses_cancellation_is_dead_lettered_like_one_that_honours_it():
    """Same record shape as the control, so a consumer of the DLQ needs no new case."""
    control = await _deliver("honour", num_delivered=2)
    out = await _deliver("return", num_delivered=2)

    assert out.disposition == ["term"], out.disposition
    assert len(out.dlq) == 1, out.dlq
    assert sorted(out.dlq[0]) == sorted(control.dlq[0]), (out.dlq[0], control.dlq[0])
    assert OVERRUN in out.dlq[0]["error"], out.dlq[0]
    assert SUPPRESSED in out.dlq[0]["error"], out.dlq[0]


# --- suppresses, then finishes late ------------------------------------------


async def test_a_handler_that_suppresses_cancellation_and_finishes_late_is_naked_and_warned_about():
    out = await _deliver("late")

    assert out.finished == [1], "the fixture must actually run on past its cancellation"
    assert out.disposition == ["nak"], out.disposition
    suppressed = _with(out.warnings, SUPPRESSED)
    assert len(suppressed) == 1, out.warnings
    assert "on_ping" in suppressed[0], suppressed


# --- suppresses, then wedges again -------------------------------------------


async def test_a_handler_that_suppresses_cancellation_and_wedges_gets_no_disposition():
    """Not naked under a handler that is still running: the redelivery would race it."""
    out = await _deliver("wedge", wait=5 * BUDGET)

    assert out.caught == [1], "the fixture must actually reach the cancellation"
    assert not out.returned, "dispatch cannot return while the handler still runs"
    assert out.disposition == [], out.disposition


async def test_a_handler_that_suppresses_cancellation_and_wedges_is_warned_about_by_name():
    out = await _deliver("wedge", wait=5 * BUDGET)

    still_running = _with(out.warnings, STILL_RUNNING)
    assert len(still_running) == 1, out.warnings
    assert "on_ping" in still_running[0], still_running
    assert f"max_processing_time ({BUDGET}s)" in still_running[0], still_running
    assert _with(out.warnings, SUPPRESSED) == [], out.warnings


async def test_CONTROL_the_still_running_warning_waits_a_second_budget():
    """Fired at twice the budget, not at the deadline: a handler cleaning up after
    an honoured cancellation is not accused of refusing it."""
    out = await _deliver("wedge", wait=1.5 * BUDGET)

    assert out.caught == [1], out.caught
    assert _with(out.warnings, STILL_RUNNING) == [], out.warnings


async def test_CONTROL_no_budget_warning_outlives_a_handler_that_honoured_it():
    """The still-running timer is cancelled when dispatch ends."""
    out = await _deliver("honour", wait=0.5, linger=3 * BUDGET)

    assert out.disposition == ["nak"], out.disposition
    assert _with(out.warnings, STILL_RUNNING) == [], out.warnings
    assert out.leaked == set(), [t.get_name() for t in out.leaked]


# --- a handler's own TimeoutError --------------------------------------------


async def test_a_handlers_own_timeout_error_is_not_reported_as_a_budget_overrun():
    """It is a handler's exception, so the record carries its type and not its text (the flag is off),
    and the overrun phrase an operator greps for is the framework's own and is not on it."""
    out = await _deliver("own_timeout", num_delivered=2)

    assert out.disposition == ["term"], out.disposition
    assert len(out.dlq) == 1, out.dlq
    assert out.dlq[0]["error"] == "TimeoutError", out.dlq[0]
    assert OVERRUN not in out.dlq[0]["error"], out.dlq[0]
    assert _with(out.warnings, OVERRUN) == [], out.warnings
