"""A best-effort publish returns to its caller without waiting for the broker.

`publish_event` awaits the send inline. Over JetStream that is an ack wait, and
`nc.jetstream()` takes nats-py's context default of five seconds -- verified in
this repository's environment: `JetStreamContext.publish` declares
`timeout=None` and falls back to `self._timeout`, which `__init__` defaults to
5. So a broker that never acknowledges holds whatever called it for that long,
and a service whose event is a wake-up ("a reader that missed one reads the
record") paid a reply's latency for a message nobody needs to wait on.

`publish_event_nowait` spawns the send as a supervised task, bounds it, and
counts what it gives up. The three properties worth stating, because each one
is a thing a caller would otherwise have to arrange:

- the caller is not held: it returns before the publish finishes;
- the message is given up on, not retried, and the fact is counted by subject
  and reason rather than swallowed -- under a bound, so a service publishing
  per entity cannot grow the tally one key per entity;
- the task is supervised, so a stopping service drains it before it closes the
  connection, rather than losing a message that was already on its way.

No broker: `nc` is a double whose publish never returns.
"""

from __future__ import annotations

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.service import (
    BEST_EFFORT_PUBLISH_SECONDS,
    OTHER_SUBJECT,
    UNCONFIRMED_SUBJECTS,
)

pytestmark = pytest.mark.unit

SUBJECT = "orders.order.processed"


class Silent:
    """A broker that accepts a publish and never answers it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.published: list[str] = []

    async def publish(self, subject, data, headers=None, **kw):
        self.started.set()
        await self.release.wait()
        self.published.append(subject)


class Refusing:
    """A broker that refuses every publish."""

    def __init__(self) -> None:
        self.attempts = 0

    async def publish(self, subject, data, headers=None, **kw):
        self.attempts += 1
        raise ConnectionResetError("no route to broker")


def service(broker) -> CliffracerService:
    svc = CliffracerService(ServiceConfig(name="sender", health_port=0))
    svc.container.nc = broker
    return svc


async def test_the_caller_returns_before_a_silent_broker_answers():
    broker = Silent()
    svc = service(broker)
    task = svc.publish_event_nowait(SUBJECT, timeout=5, order_id="1")
    # A spawned task, not a coroutine the caller must await: that is what
    # makes "the caller is not held" checkable rather than merely structural,
    # and it reds if the method is ever rewritten to await its own send.
    assert isinstance(task, asyncio.Task) and not task.done(), (
        "The caller was handed something it has to wait on"
    )
    await asyncio.wait_for(broker.started.wait(), 2)
    assert not task.done(), "The caller waited for the broker to answer"
    broker.release.set()
    await asyncio.wait_for(task, 2)
    assert broker.published == [SUBJECT]
    assert svc.unconfirmed_events == {}


async def test_a_publish_that_is_never_acknowledged_is_given_up_and_counted():
    broker = Silent()
    svc = service(broker)
    began = asyncio.get_running_loop().time()
    await asyncio.wait_for(svc.publish_event_nowait(SUBJECT, timeout=0.2), 3)
    waited = asyncio.get_running_loop().time() - began
    # Upper bound. CI p99 0.201 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.2 s,
    # 1632x the overshoot; below 3 s (the outer wait_for).
    assert waited < 2, f"The bound did not apply: the send ran {waited:.1f} s"
    assert svc.unconfirmed_events == {(SUBJECT, "timeout"): 1}, (
        "A publish that was never acknowledged was not given up and counted"
    )
    broker.release.set()


async def test_a_refused_publish_is_counted_by_the_reason_it_failed():
    broker = Refusing()
    svc = service(broker)
    for _ in range(2):
        await asyncio.wait_for(svc.publish_event_nowait(SUBJECT), 3)
    assert svc.unconfirmed_events == {(SUBJECT, "ConnectionResetError"): 2}, (
        "A refused publish was not counted by its reason"
    )
    assert broker.attempts == 2, "A given-up publish was retried"


async def test_the_task_is_supervised_so_a_stopping_service_drains_it():
    broker = Silent()
    svc = service(broker)
    task = svc.publish_event_nowait(SUBJECT, timeout=5, order_id="1")
    assert task in svc.container.lifecycle._active_tasks, (
        "A best-effort publish was not tracked, so a stop would not wait for it"
    )
    broker.release.set()
    await asyncio.wait_for(task, 2)
    assert task not in svc.container.lifecycle._active_tasks


async def test_the_default_bound_is_shorter_than_the_ack_wait_it_replaces():
    """The ceiling being replaced is nats-py's five-second context default."""
    assert 0 < BEST_EFFORT_PUBLISH_SECONDS < 5


async def test_CONTROL_an_inline_publish_does_hold_its_caller():
    """The control for the first test: `publish_event` awaited inline blocks.

    Without it, "the caller returned" proves nothing -- it would read the same
    way if the broker answered instantly.
    """
    broker = Silent()
    svc = service(broker)
    inline = asyncio.ensure_future(svc.publish_event(SUBJECT, order_id="1"))
    await asyncio.wait_for(broker.started.wait(), 2)
    await asyncio.sleep(0.05)
    assert not inline.done(), "The double answered, so the comparison is empty"
    broker.release.set()
    await asyncio.wait_for(inline, 2)


async def test_CONTROL_the_counter_starts_empty():
    """A count of one proves nothing if the counter cannot be zero."""
    assert service(Silent()).unconfirmed_events == {}


async def test_the_tally_names_a_bounded_number_of_subjects():
    """A publisher with a subject per entity must not grow the tally per entity.

    The keys are what an outage multiplies: every entity touched while the
    broker is away is a distinct subject, so an unbounded tally grows one key
    and one distinct warning for each of them, in the service that is already
    having a bad time.
    """
    svc = service(Refusing())
    entities = UNCONFIRMED_SUBJECTS + 15
    for n in range(entities):
        await asyncio.wait_for(svc.publish_event_nowait(f"orders.order.{n}"), 3)
    tally = svc.unconfirmed_events
    named = {subject for subject, _ in tally}
    assert len(named) == UNCONFIRMED_SUBJECTS + 1, (
        f"The tally grew to {len(named)} subjects for {entities} entities"
    )
    assert OTHER_SUBJECT in named
    assert sum(tally.values()) == entities, "A counted publish was lost in the overflow"
    assert tally[(OTHER_SUBJECT, "ConnectionResetError")] == entities - UNCONFIRMED_SUBJECTS


async def test_a_named_subject_keeps_its_own_count_after_the_bound_is_reached():
    """A steady publisher must not lose its count to somebody else's burst."""
    svc = service(Refusing())
    await asyncio.wait_for(svc.publish_event_nowait(SUBJECT), 3)
    for n in range(UNCONFIRMED_SUBJECTS + 5):
        await asyncio.wait_for(svc.publish_event_nowait(f"orders.order.{n}"), 3)
    await asyncio.wait_for(svc.publish_event_nowait(SUBJECT), 3)
    assert svc.unconfirmed_events[(SUBJECT, "ConnectionResetError")] == 2, (
        "A subject the tally already named lost its count to the overflow"
    )
