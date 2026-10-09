"""A pull consumer's fetched batch is dispatched concurrently, bounded by config.

`pull_once` fetched `jetstream_pull_batch` messages and then awaited each
handler before starting the next, so a batch of eight held one message in
flight. Two consequences: `max_event_concurrency` never applied on the pull
path -- `get_event_semaphore()` was consulted only by the PUSH callback -- and
`jetstream_max_ack_pending`, which the docs call "how much work the broker
hands this replica at once", bounded nothing, because the replica never held
more than one message.

MEASURED BEFORE THE FIX, driving the real `_pull_once` with eight messages and
a 100ms handler:

    max_event_concurrency=None   peak_concurrency=1   returned after 0.81s
    max_event_concurrency=8      peak_concurrency=1   returned after 0.81s
    max_event_concurrency=1      peak_concurrency=1   returned after 0.81s

and after:

    max_event_concurrency=None   peak_concurrency=8   returned after 0.11s
    max_event_concurrency=8      peak_concurrency=8   returned after 0.11s
    max_event_concurrency=1      peak_concurrency=1   returned after 0.81s

THE DISCRIMINATOR HERE IS ORDERING, NOT DURATION. No handler can finish until
every handler has started, because they all wait on one event that the last
arrival sets. Serial dispatch cannot reach that state at all, on any machine:
the first handler waits for a release that only the eighth can trigger. The
timeouts below are fail-safes that turn a deadlock into a legible failure, not
measurements -- nothing here asserts that anything was fast.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

BATCH = 8

# A fail-safe, not a measurement: it separates "they all started" from "they
# cannot all start". The handlers do no work, so the state is reached in
# milliseconds when dispatch is concurrent.
ALL_START_WITHIN = 10.0


class Counters:
    """What the handlers record, so the assertions read a count and not a clock."""

    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.live = 0
        self.peak = 0
        self.started = 0
        self.completed = 0
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    def enter(self) -> None:
        self.started += 1
        self.live += 1
        self.peak = max(self.peak, self.live)
        if self.live >= self.expected:
            self.all_started.set()

    def leave(self) -> None:
        self.live -= 1
        self.completed += 1


def _config(**overrides) -> ServiceConfig:
    return ServiceConfig(
        name="puller",
        health_port=0,
        health_listener=False,
        jetstream_enabled=True,
        jetstream_pull_batch=BATCH,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
        **overrides,
    )


def _msg(seq: int) -> AsyncMock:
    # The payload and the handler signature are copied from
    # tests/unit/test_pull_consumers.py. A handler that does not accept `seq`
    # makes every message INVALID, which dead-letters the batch and runs no
    # handler at all -- a version of this file did exactly that and reported
    # peak_concurrency=0.
    msg = AsyncMock()
    msg.subject = "events.ping"
    msg.data = b'{"seq": %d}' % seq
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=1)
    return msg


def _service(counters: Counters, **overrides) -> CliffracerService:
    class S(CliffracerService):
        @listener("events.ping", durable="pinger", pull=True)
        async def on_ping(self, subject: str, seq: int = 1) -> None:
            counters.enter()
            try:
                # Nobody leaves until everybody has arrived.
                await counters.release.wait()
            finally:
                counters.leave()

    svc = S(_config(**overrides))
    svc._discover_handlers()
    return svc


def _sub(count: int = BATCH) -> AsyncMock:
    sub = AsyncMock()
    sub.fetch.return_value = [_msg(i) for i in range(count)]
    return sub


async def _all_started(counters: Counters, *, what: str) -> None:
    try:
        await asyncio.wait_for(counters.all_started.wait(), timeout=ALL_START_WITHIN)
    except TimeoutError:
        counters.release.set()
        pytest.fail(
            f"{what}: {counters.started} of {counters.expected} handlers had "
            f"started and {counters.live} were running. No handler can finish "
            "until all have started, so this is serial dispatch rather than a "
            "slow one"
        )


# --- the batch runs concurrently ---------------------------------------------


async def test_every_message_in_a_batch_is_running_before_any_of_them_finishes():
    """The whole of the defect, stated as the state serial dispatch cannot reach."""
    counters = Counters(BATCH)
    svc = _service(counters)
    sub = _sub()

    dispatching = asyncio.create_task(svc.container._pull_once(sub))
    await _all_started(counters, what="an unbounded pull batch")
    counters.release.set()
    await asyncio.wait_for(dispatching, timeout=ALL_START_WITHIN)

    assert counters.peak == BATCH, counters.peak
    assert counters.completed == BATCH, counters.completed


async def test_the_batch_is_bounded_by_max_event_concurrency():
    """Concurrent, but not unbounded: the push path's semaphore now applies here.

    Both sides of the bound: never more than the limit, and the limit is
    actually reached -- a dispatcher that ran two at a time would satisfy "at
    most three" on its own.
    """
    limit = 3
    counters = Counters(limit)
    svc = _service(counters, max_event_concurrency=limit)
    sub = _sub()

    dispatching = asyncio.create_task(svc.container._pull_once(sub))
    await _all_started(counters, what=f"a batch bounded at {limit}")

    assert counters.peak == limit, f"{counters.peak} handlers ran at once under a limit of {limit}"

    counters.release.set()
    await asyncio.wait_for(dispatching, timeout=ALL_START_WITHIN)
    assert counters.completed == BATCH, (
        f"only {counters.completed} of {BATCH} messages were dispatched once the permits were free"
    )
    assert counters.peak == limit, f"the limit was exceeded later in the batch: {counters.peak}"


async def test_CONTROL_a_limit_of_one_still_serialises():
    """The setting still means what it says.

    Without this, "the batch is concurrent" could be satisfied by ignoring the
    semaphore -- which is the defect with its sign flipped.
    """
    counters = Counters(1)
    svc = _service(counters, max_event_concurrency=1)
    sub = _sub()

    dispatching = asyncio.create_task(svc.container._pull_once(sub))
    await _all_started(counters, what="a batch bounded at 1")
    await asyncio.sleep(0)  # any second handler would start here

    assert counters.peak == 1, f"{counters.peak} handlers ran at once under a limit of 1"
    assert counters.started == 1, counters.started

    counters.release.set()
    await asyncio.wait_for(dispatching, timeout=ALL_START_WITHIN)
    assert counters.completed == BATCH, counters.completed


# --- shutdown still waits for what is in flight ------------------------------


async def test_the_drain_still_waits_for_everything_in_flight():
    """Cancelling the fetch loop must not take the handlers with it.

    `LifecycleManager.stop_internal` cancels subscriptions as step 3 and drains
    `_active_tasks` as step 4, so the handlers are the supervisor's to finish,
    not the batch's to abandon. `pull_once` shields its gather for that reason:
    without the shield, cancelling the loop cancels handlers mid-message, and a
    JetStream message dropped mid-handler is redelivered rather than acked.
    """
    counters = Counters(BATCH)
    svc = _service(counters)
    sub = _sub()

    dispatching = asyncio.create_task(svc.container._pull_once(sub))
    await _all_started(counters, what="a batch being drained")

    dispatching.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatching

    assert counters.completed == 0, "a handler finished before the drain even began"
    assert counters.live == BATCH, counters.live

    counters.release.set()
    await asyncio.wait_for(
        svc.container.lifecycle.drain_active_tasks(timeout=ALL_START_WITHIN),
        timeout=ALL_START_WITHIN * 2,
    )

    assert counters.completed == BATCH, (
        f"the drain returned with {counters.completed} of {BATCH} handlers "
        "finished, so shutdown abandoned work that was in flight"
    )


# --- controls ----------------------------------------------------------------


async def test_CONTROL_the_fetch_asks_for_the_configured_batch_size():
    """So the tests above are about dispatching a batch, not about a fake that
    returns eight whatever it is asked for."""
    counters = Counters(BATCH)
    svc = _service(counters)
    sub = _sub()

    dispatching = asyncio.create_task(svc.container._pull_once(sub))
    await _all_started(counters, what="a batch")
    counters.release.set()
    await asyncio.wait_for(dispatching, timeout=ALL_START_WITHIN)

    assert sub.fetch.await_args.args[0] == BATCH, sub.fetch.await_args


async def test_CONTROL_a_handler_that_returns_immediately_still_acks_every_message():
    """The ordinary path, with none of the blocking above.

    Concurrency must not change the outcome per message: eight acks, no naks,
    no terminations.
    """
    handled: list[int] = []

    class S(CliffracerService):
        @listener("events.ping", durable="pinger", pull=True)
        async def on_ping(self, subject: str, seq: int = 1) -> None:
            handled.append(seq)

    svc = S(_config())
    svc._discover_handlers()
    sub = _sub()
    msgs = sub.fetch.return_value

    count = await asyncio.wait_for(svc.container._pull_once(sub), timeout=ALL_START_WITHIN)

    assert count == BATCH
    assert sorted(handled) == list(range(BATCH)), handled
    for msg in msgs:
        assert msg.ack.await_count == 1, msg.ack.await_count
        assert msg.nak.await_count == 0
        assert msg.term.await_count == 0
