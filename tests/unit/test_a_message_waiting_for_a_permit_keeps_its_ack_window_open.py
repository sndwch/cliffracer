"""A JetStream message that waits for a concurrency permit is pulsed while it waits.

The server's `ack_wait` runs from delivery, not from the start of the handler. With
`max_event_concurrency` below the number of messages in flight, a delivered message waits for a
permit, and the in-progress heartbeat began only once the handler did, so a message that waited
longer than `ack_wait` was redelivered to a replica that still held it and its handler ran again.

The deliveries here are real `nats.aio.msg.Msg` objects with an ack subject. What each one told the
server, and when, is read from what it published on that subject: a message is safe from
redelivery while the longest silence between delivery, its pulses and its ack is shorter than
`ack_wait`.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nats.aio.msg import Msg

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

ACK_WAIT = 0.4
HANDLER_SECONDS = 0.3
# Pulses come every ACK_WAIT / 2. Four messages at one permit wait 0, 0.3, 0.6 and 0.9 seconds.
LONGEST_SILENCE_ALLOWED = ACK_WAIT * 0.75

STATE: dict = {}


class Slow(CliffracerService):
    @listener("events.order", durable="orders-d")
    async def on_order(self, subject: str, number: int = 0) -> None:
        STATE["running"] += 1
        STATE["most_running"] = max(STATE["most_running"], STATE["running"])
        STATE["handled"].append(number)
        try:
            await asyncio.sleep(HANDLER_SECONDS)
        finally:
            STATE["running"] -= 1


class Unbroked(CliffracerService):
    """The same handler without a durable, for a service started without JetStream."""

    @listener("events.order", fanout=True)
    async def on_order(self, subject: str, number: int = 0) -> None:
        await Slow.on_order(self, subject, number)  # type: ignore[arg-type]


def _config(**extra):
    return ServiceConfig(
        name="orders",
        health_port=0,
        jetstream_enabled=True,
        jetstream_ack_wait=ACK_WAIT,
        max_event_concurrency=1,
        jetstream_streams=[
            StreamSpec(name="EVENTS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
        **extra,
    )


class Delivery:
    """A real `Msg` whose published settlements are timestamped."""

    def __init__(self, number: int) -> None:
        self.number = number
        self.sent: list[tuple[str, float]] = []
        client = MagicMock()

        async def publish(subject, payload=b"", *args, **kwargs):
            kind = (
                "nak"
                if payload.startswith(b"-NAK")
                else "term"
                if payload.startswith(b"+TERM")
                else "progress"
                if payload.startswith(b"+WPI")
                else "ack"
            )
            self.sent.append((kind, time.monotonic()))

        client.publish = AsyncMock(side_effect=publish)
        self.received = time.monotonic()
        self.msg = Msg(
            _client=client,
            subject="events.order",
            reply=f"$JS.ACK.EVENTS.orders-d.1.{number + 1}.{number + 1}.1700000000000000000.0",
            data=json.dumps({"number": number}).encode(),
            headers={"Content-Type": "application/json"},
        )

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.sent]

    def longest_silence(self) -> float:
        """The longest stretch between delivery, a pulse or the ack, with none of them."""
        moments = [self.received] + [at for _, at in self.sent]
        return max(later - earlier for earlier, later in zip(moments, moments[1:], strict=False))


async def _service():
    svc = Slow(_config())
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    STATE.clear()
    STATE.update(running=0, most_running=0, handled=[])
    return svc


async def _until_handled(svc) -> None:
    tasks = list(svc.container._active_tasks)
    assert tasks, "the callback spawned nothing to wait for"
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)


def _assert_every_message_was_kept_alive_and_handled_once(deliveries):
    assert sorted(STATE["handled"]) == [d.number for d in deliveries]
    for delivery in deliveries:
        assert delivery.kinds()[-1] == "ack", (delivery.number, delivery.kinds())
        assert delivery.longest_silence() < LONGEST_SILENCE_ALLOWED, (
            f"message {delivery.number} went {delivery.longest_silence():.2f}s without a sign of "
            f"life, and the server redelivers after {ACK_WAIT}s"
        )


async def test_push_messages_waiting_for_a_permit_are_pulsed_until_their_handler_runs():
    svc = await _service()
    callback = svc.container.dispatcher.make_jetstream_event_callback("events.order")
    deliveries = [Delivery(n) for n in range(4)]

    async def subscription() -> None:
        # nats-py hands a subscription's messages to its callback one at a time, so a callback
        # that waits holds back every message behind it, delivered or not.
        for delivery in deliveries:
            await callback(delivery.msg)

    await asyncio.wait_for(subscription(), timeout=10)
    await _until_handled(svc)

    _assert_every_message_was_kept_alive_and_handled_once(deliveries)
    assert STATE["most_running"] == 1


async def test_pull_messages_waiting_for_a_permit_are_pulsed_until_their_handler_runs():
    svc = await _service()
    deliveries = [Delivery(n) for n in range(4)]
    sub = MagicMock()
    sub.fetch = AsyncMock(return_value=[d.msg for d in deliveries])

    await asyncio.wait_for(svc.container._pull_once(sub, pattern="events.order"), timeout=10)

    _assert_every_message_was_kept_alive_and_handled_once(deliveries)
    assert STATE["most_running"] == 1


async def test_a_message_that_gets_a_permit_at_once_is_not_pulsed_before_its_handler_runs():
    # CONTROL: the permit is free, so nothing waits and the first pulse is the handler's own,
    # one interval after it starts.
    svc = await _service()
    callback = svc.container.dispatcher.make_jetstream_event_callback("events.order")
    only = Delivery(0)

    await callback(only.msg)
    await _until_handled(svc)

    assert only.kinds() == ["progress", "ack"]
    assert only.sent[0][1] - only.received >= ACK_WAIT / 2 * 0.9


async def test_a_cancelled_waiter_returns_nothing_and_keeps_every_permit_usable():
    svc = await _service()
    jetstream = svc.container.dispatcher.jetstream
    deliveries = [Delivery(n) for n in range(3)]
    tasks = [
        asyncio.create_task(jetstream._bounded_handle_jetstream_event(d.msg, "events.order"))
        for d in deliveries
    ]
    await asyncio.sleep(0.05)  # the first holds the permit, the others wait, pulsed
    tasks[1].cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    assert STATE["handled"] == [0, 2]
    assert deliveries[1].kinds() == [] or "ack" not in deliveries[1].kinds()
    assert deliveries[0].kinds()[-1] == "ack" and deliveries[2].kinds()[-1] == "ack"
    sem = svc.container.dispatcher.events.get_event_semaphore()
    assert sem is not None and not sem.locked(), "a permit was lost"
    sem_value = sem._value  # the count of permits free: one, no more and no fewer
    assert sem_value == 1


@pytest.fixture
async def started():
    """A service that has been started over a connection that answers every call."""
    svc = Unbroked(ServiceConfig(name="orders", health_port=0, max_event_concurrency=1))
    svc.container.nc = AsyncMock()
    svc.container.nc.is_connected = True
    svc.container.nc.is_closed = False
    svc.container.nc.is_draining = False
    with (
        patch.object(svc, "connect", new_callable=AsyncMock),
        patch.object(svc, "disconnect", new_callable=AsyncMock),
    ):
        await svc.start()
        STATE.clear()
        STATE.update(running=0, most_running=0, handled=[])
        yield svc
        await svc.stop()


async def test_a_stopping_service_starts_no_message_that_was_waiting_for_a_permit(started):
    callback = started.container.dispatcher.make_jetstream_event_callback("events.order")
    deliveries = [Delivery(n) for n in range(4)]
    for delivery in deliveries:
        await callback(delivery.msg)
    await asyncio.sleep(0.05)  # the first handler is running, the other three wait

    await asyncio.wait_for(started.stop(), timeout=10)

    assert STATE["handled"] == [0], "a message that waited was started by a stopping service"
    assert deliveries[0].kinds()[-1] == "ack", "the work that was running is finished and settled"
    for waiter in deliveries[1:]:
        assert set(waiter.kinds()) <= {"progress"}, (
            "a message left for redelivery is not acknowledged, nak'd or terminated",
            waiter.number,
            waiter.kinds(),
        )
    assert started.container._active_tasks == frozenset()
    sem = started.container.dispatcher.events.get_event_semaphore()
    assert sem is not None and sem._value == 1, "a permit was lost"


async def test_a_message_that_arrives_while_the_service_is_stopping_is_not_started(started):
    callback = started.container.dispatcher.make_jetstream_event_callback("events.order")
    first, late = Delivery(0), Delivery(1)
    await callback(first.msg)
    await asyncio.sleep(0.05)
    stopping = asyncio.create_task(started.stop())
    await asyncio.sleep(0.05)  # the stop has begun and the first handler is still running

    await callback(late.msg)
    await asyncio.wait_for(stopping, timeout=10)

    assert STATE["handled"] == [0]
    assert set(late.kinds()) <= {"progress"}


async def test_CONTROL_a_service_that_is_not_stopping_starts_every_message_that_waited(started):
    callback = started.container.dispatcher.make_jetstream_event_callback("events.order")
    deliveries = [Delivery(n) for n in range(3)]
    for delivery in deliveries:
        await callback(delivery.msg)

    await _until_handled(started)

    assert sorted(STATE["handled"]) == [0, 1, 2]
    assert all(d.kinds()[-1] == "ack" for d in deliveries)


async def test_a_shutdown_out_of_time_cancels_the_waiters_and_leaves_them_for_redelivery():
    svc = await _service()
    callback = svc.container.dispatcher.make_jetstream_event_callback("events.order")
    deliveries = [Delivery(n) for n in range(4)]
    for delivery in deliveries:
        await callback(delivery.msg)
    await asyncio.sleep(0.05)  # the first handler is running, the other three wait

    await asyncio.wait_for(svc.container.lifecycle.drain_active_tasks(timeout=0.1), timeout=15)

    assert STATE["handled"] == [0], "a waiter ran after the drain ran out of time"
    assert [d.kinds() for d in deliveries[1:]] == [
        ["progress"] * len(d.kinds()) for d in deliveries[1:]
    ]
    assert svc.container._active_tasks == frozenset()
    sem = svc.container.dispatcher.events.get_event_semaphore()
    assert sem is not None and sem._value == 1, "a permit was lost to the cancelled tasks"


async def test_a_handler_that_raises_returns_its_permit():
    svc = await _service()
    jetstream = svc.container.dispatcher.jetstream
    first = Delivery(0)
    jetstream.handle_jetstream_event = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await jetstream._bounded_handle_jetstream_event(first.msg, "events.order")

    sem = svc.container.dispatcher.events.get_event_semaphore()
    assert sem is not None and sem._value == 1


# --- a wait at the listener's own limit -----------------------------------------------------
#
# A listener's `max_concurrency` is a permit of the method's, taken before any service permit.
# With no `max_event_concurrency` at all, a message waits at the method alone, and it must be
# pulsed as one waiting at the service's limit is.


class SlowAtTheMethod(CliffracerService):
    @listener("events.order", durable="orders-d", max_concurrency=1)
    async def on_order(self, subject: str, number: int = 0) -> None:
        await Slow.on_order(self, subject, number)  # type: ignore[arg-type]


async def _method_limited_service():
    svc = SlowAtTheMethod(
        ServiceConfig(
            name="orders",
            health_port=0,
            jetstream_enabled=True,
            jetstream_ack_wait=ACK_WAIT,
            jetstream_streams=[
                StreamSpec(name="EVENTS", subjects=["events.*"]),
                StreamSpec(name="DLQ", subjects=["dlq.*"]),
            ],
        )
    )
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc.container.discover_handlers()
    assert svc.container.dispatcher.events.get_event_semaphore() is None, "a service limit is set"
    STATE.clear()
    STATE.update(running=0, most_running=0, handled=[])
    return svc


async def test_push_messages_waiting_at_their_methods_limit_are_pulsed_until_it_runs_them():
    svc = await _method_limited_service()
    callback = svc.container.dispatcher.make_jetstream_event_callback("events.order")
    deliveries = [Delivery(n) for n in range(4)]

    async def subscription() -> None:
        for delivery in deliveries:
            await callback(delivery.msg)

    await asyncio.wait_for(subscription(), timeout=10)
    await _until_handled(svc)

    _assert_every_message_was_kept_alive_and_handled_once(deliveries)
    assert STATE["most_running"] == 1


async def test_pull_messages_waiting_at_their_methods_limit_are_pulsed_until_it_runs_them():
    svc = await _method_limited_service()
    deliveries = [Delivery(n) for n in range(4)]
    sub = MagicMock()
    sub.fetch = AsyncMock(return_value=[d.msg for d in deliveries])

    await asyncio.wait_for(svc.container._pull_once(sub, pattern="events.order"), timeout=10)

    _assert_every_message_was_kept_alive_and_handled_once(deliveries)
    assert STATE["most_running"] == 1
