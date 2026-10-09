"""A scheduled event reaches its durable listener when it is due, live, on nats-server 2.12+.

Collected only by `scripts/check_message_schedules.py`, which starts a pinned nats-server 2.12
container, points `$CLIFFRACER_TEST_NATS_URL` at it and sets `$CLIFFRACER_TEST_MESSAGE_SCHEDULES`;
everywhere else these rows are deselected, not skipped, because the suite's broker is older than
the feature. The gate refuses a run in which any of them skipped.
"""

import asyncio
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

import nats
import pytest
from nats.js.api import ConsumerConfig, DeliverPolicy

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, listener
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.testing.waiting import wait_until

pytestmark = [pytest.mark.integration, pytest.mark.nats_required, pytest.mark.message_schedules]

#: The outer bound on a delivery that only a defect can make long.
WITHIN = 20.0


def _service(target: str, *, tag: str | None = None) -> CliffracerService:
    """A service with one durable listener on `target`, in a stream that allows schedules.

    Two services built with one `tag` declare the same stream and durable, so the second reads
    what the first scheduled.
    """
    tag = tag or uuid.uuid4().hex[:8]

    class Reminders(CliffracerService):
        def __init__(self) -> None:
            super().__init__(
                ServiceConfig(
                    name=f"itest_sched_{tag}",
                    health_port=0,
                    jetstream_enabled=True,
                    jetstream_streams=[
                        StreamSpec(
                            name=f"ITEST_SCHED_{tag}",
                            subjects=[target, f"_sched.{target}.*"],
                            allow_msg_schedules=True,
                        ),
                        StreamSpec(
                            name=f"ITEST_SCHED_DLQ_{tag}", subjects=[f"dlq.itest_sched_{tag}"]
                        ),
                    ],
                    dlq_subject=f"dlq.itest_sched_{tag}",
                )
            )
            self.received: list[tuple[str, datetime]] = []

        @listener(target, durable=f"itest_sched_{tag}")
        async def on_due(self, subject: str, note: str) -> None:
            self.received.append((note, datetime.now(UTC)))

    return Reminders()


async def test_a_scheduled_event_reaches_its_listener_when_it_is_due():
    service = _service(f"itest.sched.due.{uuid.uuid4().hex[:8]}")
    target = service.config.jetstream_streams[0].subjects[0]
    await service.start()
    try:
        due = datetime.now(UTC) + timedelta(seconds=2)
        await service.schedules.publish_at(target, when=due, key="r1", note="due")
        await wait_until(lambda: service.received, within=WITHIN, reason="the scheduled event")
        ((note, arrived),) = service.received
    finally:
        await service.stop()

    assert note == "due"
    # The broker fires on whole instants it reads from the header; it is not early by more than
    # the second it rounds within, and the outer wait bounds it late.
    assert arrived >= due - timedelta(seconds=1)


async def test_scheduling_again_under_the_same_key_replaces_the_schedule():
    service = _service(f"itest.sched.replace.{uuid.uuid4().hex[:8]}")
    target = service.config.jetstream_streams[0].subjects[0]
    await service.start()
    try:
        now = datetime.now(UTC)
        await service.schedules.publish_at(
            target, when=now + timedelta(seconds=2), key="r1", note="first"
        )
        await service.schedules.publish_at(
            target, when=now + timedelta(seconds=3), key="r1", note="second"
        )
        # The replaced schedule was due first, so had it survived it would be the first to arrive.
        await wait_until(lambda: service.received, within=WITHIN, reason="the replacing event")
    finally:
        await service.stop()

    assert [note for note, _ in service.received] == ["second"]


async def test_a_cancelled_schedule_writes_nothing():
    service = _service(f"itest.sched.cancel.{uuid.uuid4().hex[:8]}")
    target = service.config.jetstream_streams[0].subjects[0]
    await service.start()
    try:
        await service.schedules.publish_at(
            target, when=datetime.now(UTC) + timedelta(seconds=2), key="gone", note="cancelled"
        )
        await service.schedules.cancel(target, key="gone")
        await service.schedules.publish_in(
            target, after=timedelta(seconds=3), key="kept", note="kept"
        )
        # The kept schedule is due after the cancelled one, so its arrival is the barrier past
        # which the cancelled one would have arrived too.
        await wait_until(lambda: service.received, within=WITHIN, reason="the kept event")
    finally:
        await service.stop()

    assert [note for note, _ in service.received] == ["kept"]


async def test_a_when_already_past_is_written_at_once():
    service = _service(f"itest.sched.past.{uuid.uuid4().hex[:8]}")
    target = service.config.jetstream_streams[0].subjects[0]
    await service.start()
    try:
        await service.schedules.publish_at(
            target, when=datetime.now(UTC) - timedelta(hours=1), key="late", note="late"
        )
        await wait_until(lambda: service.received, within=WITHIN, reason="the past-due event")
    finally:
        await service.stop()

    assert [note for note, _ in service.received] == ["late"]


# --- what the fired copy carries, who sees it, and its redelivery ----------------------------


async def _fired_copy_consumer(service: CliffracerService, target: str, tag: str):
    """A raw pull consumer on the target as the broker holds it (the run's prefix applied), with
    a short ack wait, and its connection."""
    nc = await nats.connect(service.config.nats_url)
    js = nc.jetstream()
    sub = await js.pull_subscribe(
        HandlerDiscovery.with_namespace(service.config, target),
        durable=f"raw_{tag}",
        stream=service.config.effective_jetstream_streams[0].name,
        config=ConsumerConfig(ack_wait=1.0, deliver_policy=DeliverPolicy.ALL),
    )
    return nc, sub


async def _fetch_one(sub) -> object:
    async def one():
        while True:
            try:
                (msg,) = await sub.fetch(1, timeout=1.0)
                return msg
            except TimeoutError:
                continue

    return await asyncio.wait_for(one(), WITHIN)


async def test_the_fired_copy_keeps_its_content_type_and_correlation_id_and_adds_the_scheduler():
    tag = uuid.uuid4().hex[:8]
    service = _service(f"itest.sched.headers.{tag}")
    target = service.config.jetstream_streams[0].subjects[0]
    await service.start()
    nc = None
    try:
        nc, sub = await _fired_copy_consumer(service, target, tag)
        await service.schedules.publish_in(
            target,
            after=timedelta(seconds=1),
            key="h1",
            note="headers",
            correlation_id="cid-sched-headers",
            idempotency_key="once-only",
        )
        fired = await _fetch_one(sub)
        await fired.ack()
    finally:
        if nc is not None:
            await nc.close()
        await service.stop()

    headers = dict(fired.headers or {})
    assert headers.get("Content-Type", "").startswith("application/json"), headers
    assert headers.get("X-Correlation-ID") == "cid-sched-headers", headers
    assert headers.get("Nats-Scheduler", "").endswith(f".{target}.h1"), headers
    assert "Nats-Schedule-Next" in headers, headers
    assert "Nats-Msg-Id" not in headers, headers


async def test_a_core_subscription_does_not_see_the_fired_copy():
    tag = uuid.uuid4().hex[:8]
    service = _service(f"itest.sched.core.{tag}")
    target = service.config.jetstream_streams[0].subjects[0]
    await service.start()
    nc = await nats.connect(service.config.nats_url)
    seen: list[object] = []

    async def heard(msg) -> None:
        seen.append(msg)

    try:
        await nc.subscribe(HandlerDiscovery.with_namespace(service.config, target), cb=heard)
        await nc.flush()
        await service.schedules.publish_in(target, after=timedelta(seconds=1), key="c1", note="c")
        # The durable listener's delivery is the barrier: the copy is written by then.
        await wait_until(lambda: service.received, within=WITHIN, reason="the scheduled event")
        await nc.flush()
    finally:
        await nc.close()
        await service.stop()

    assert [note for note, _ in service.received] == ["c"]
    assert seen == [], "a core subscription saw the scheduled copy"


async def test_a_redelivered_fired_copy_carries_the_same_scheduler_and_stream_sequence():
    tag = uuid.uuid4().hex[:8]
    service = _service(f"itest.sched.redeliver.{tag}")
    target = service.config.jetstream_streams[0].subjects[0]
    await service.start()
    nc = None
    try:
        nc, sub = await _fired_copy_consumer(service, target, tag)
        await service.schedules.publish_in(target, after=timedelta(seconds=1), key="r1", note="r")
        first = await _fetch_one(sub)  # not acknowledged: the broker redelivers after ack_wait
        again = await _fetch_one(sub)
        await again.ack()
    finally:
        if nc is not None:
            await nc.close()
        await service.stop()

    assert (first.metadata.num_delivered, again.metadata.num_delivered) == (1, 2)
    assert again.metadata.sequence.stream == first.metadata.sequence.stream
    assert again.headers["Nats-Scheduler"] == first.headers["Nats-Scheduler"]


async def test_CONTROL_a_core_subscription_sees_an_event_published_on_the_same_subject():
    tag = uuid.uuid4().hex[:8]
    service = _service(f"itest.sched.corectl.{tag}")
    target = service.config.jetstream_streams[0].subjects[0]
    await service.start()
    nc = await nats.connect(service.config.nats_url)
    seen: list[object] = []

    async def heard(msg) -> None:
        seen.append(msg)

    try:
        await nc.subscribe(HandlerDiscovery.with_namespace(service.config, target), cb=heard)
        await nc.flush()
        await service.publish_event(target, note="plain")
        await wait_until(lambda: seen, within=WITHIN, reason="the plain event on the core subject")
    finally:
        await nc.close()
        await service.stop()

    assert len(seen) == 1


async def test_CONTROL_the_stored_schedule_carries_the_msg_id_its_fired_copy_drops():
    tag = uuid.uuid4().hex[:8]
    service = _service(f"itest.sched.msgid.{tag}")
    target = service.config.jetstream_streams[0].subjects[0]
    await service.start()
    nc = await nats.connect(service.config.nats_url)
    try:
        await service.schedules.publish_in(
            target, after=timedelta(hours=1), key="m1", note="later", idempotency_key="once-only"
        )
        stored = await nc.jetstream().get_last_msg(
            service.config.effective_jetstream_streams[0].name,
            HandlerDiscovery.with_namespace(service.config, f"_sched.{target}.m1"),
        )
        await service.schedules.cancel(target, key="m1")
    finally:
        await nc.close()
        await service.stop()

    headers = dict(stored.headers or {})
    assert "Nats-Msg-Id" in headers, headers
    assert headers.get("Nats-Schedule", "").startswith("@at "), headers


# --- a schedule across a broker restart -------------------------------------------------------
#
# These rows restart the broker, so they need the gate's own container, named in
# `$CLIFFRACER_TEST_SCHEDULE_BROKER`; the gate maps it to a fixed loopback port, so it answers at the
# same address after a restart, and a fresh service there reads what the first one scheduled.

BROKER_ENV = "CLIFFRACER_TEST_SCHEDULE_BROKER"


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout.strip()


def _the_gates_broker() -> str:
    """The container the gate created. Skipped loudly without it, which the gate refuses."""
    container = os.environ.get(BROKER_ENV)
    if not container:
        pytest.skip(
            f"NOT RUN: ${BROKER_ENV} is unset; this row restarts the broker, so it runs only on "
            f"the container scripts/check_message_schedules.py created"
        )
    return container


async def _answering(url: str) -> None:
    """Return once the broker at `url` accepts a connection again."""

    async def answers() -> None:
        while True:
            try:
                nc = await nats.connect(url, connect_timeout=0.5, allow_reconnect=False)
            except Exception:
                await asyncio.sleep(0.2)
                continue
            await nc.close()
            return

    await asyncio.wait_for(answers(), WITHIN)


async def _server_id(url: str) -> str:
    """The id of the server answering at `url`. A restarted broker is a new server with a new id
    on the same port, so an id that did not change means there was no outage to survive.

    Read from the INFO the server sent, which nats-py keeps in `_server_info`: it has no public
    accessor for the id."""
    nc = await nats.connect(url, connect_timeout=1.0, allow_reconnect=False)
    try:
        return nc._server_info["server_id"]
    finally:
        await nc.close()


async def test_a_schedule_survives_a_broker_restart_and_a_new_service():
    container = _the_gates_broker()
    tag = uuid.uuid4().hex[:8]
    target = f"itest.sched.restart.{tag}"
    first = _service(target, tag=tag)
    await first.start()
    try:
        await first.schedules.publish_in(target, after=timedelta(seconds=8), key="r", note="kept")
    finally:
        await first.stop()

    before = await _server_id(first.config.nats_url)
    await asyncio.to_thread(_docker, "restart", container)
    await _answering(first.config.nats_url)
    assert await _server_id(first.config.nats_url) != before, "the broker was not restarted"
    second = _service(target, tag=tag)
    await second.start()
    try:
        await wait_until(lambda: second.received, within=WITHIN, reason="the event after restart")
    finally:
        await second.stop()

    assert [note for note, _ in second.received] == ["kept"]


async def test_a_schedule_due_while_the_broker_is_down_is_written_when_it_is_back():
    container = _the_gates_broker()
    tag = uuid.uuid4().hex[:8]
    target = f"itest.sched.down.{tag}"
    first = _service(target, tag=tag)
    await first.start()
    try:
        due = datetime.now(UTC) + timedelta(seconds=6)
        await first.schedules.publish_at(target, when=due, key="d", note="late")
    finally:
        await first.stop()

    before = await _server_id(first.config.nats_url)
    await asyncio.to_thread(_docker, "stop", container)
    down = datetime.now(UTC)
    # The row is about a schedule that falls due while the broker is down, so the broker must be
    # down first; a stop slower than the lead would test a schedule that fired before it.
    assert down < due, f"the broker went down at {down}, after the schedule was due at {due}"
    await asyncio.sleep((due - down).total_seconds() + 2.0)
    await asyncio.to_thread(_docker, "start", container)
    await _answering(first.config.nats_url)
    assert await _server_id(first.config.nats_url) != before, "the broker was not stopped"
    second = _service(target, tag=tag)
    await second.start()
    try:
        await wait_until(lambda: second.received, within=WITHIN, reason="the event due while down")
    finally:
        await second.stop()

    assert [note for note, _ in second.received] == ["late"]
