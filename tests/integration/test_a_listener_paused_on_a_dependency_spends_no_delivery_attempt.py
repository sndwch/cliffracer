"""A listener paused because a dependency it names is down takes nothing, spends nothing, and resumes.

`pause_when_down` exists so that messages wait in the stream while a dependency is down, instead of
being delivered, failed, NAKed into backoff and dead-lettered. That only holds if the pause stops
the broker delivering to this replica and costs the messages no delivery attempt, which is a
property of the broker and the client, not of the in-memory side. So it is checked here: messages
published while the listener is paused are not handed to it, the durable reports nothing in flight
and nothing redelivered across three `ack_wait`s, and once the dependency is back each message is
handled exactly once. Both durable shapes: push (bound with a deliver group, as the service binds
it) and pull.

Runs against a throwaway local broker only.
"""

import asyncio

import nats
import pytest

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, dependency, listener
from cliffracer.testing.waiting import wait_until
from tests.broker_isolation import prefixed_name
from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

ACK_WAIT = 1.0
MESSAGES = 3
STREAM = "PAUSEQ"


def _config(name: str) -> ServiceConfig:
    return ServiceConfig(
        name=name,
        health_port=0,
        jetstream_enabled=True,
        jetstream_ack_wait=ACK_WAIT,
        dependency_probe_interval=0.2,
        dependency_pause_after=1,
        dependency_resume_after=1,
        jetstream_streams=[
            StreamSpec(name=STREAM, subjects=["itest.pause.*"]),
            StreamSpec(name="PAUSEQ_DLQ", subjects=["dlq.*"]),
        ],
    )


@pytest.fixture(autouse=True)
async def _clean_streams():
    async def _purge() -> None:
        nc = await nats.connect(broker_url())
        js = nc.jetstream()
        for name in (STREAM, "PAUSEQ_DLQ"):
            try:
                await js.delete_stream(prefixed_name(name))
            except Exception:
                pass
        await nc.close()

    await _purge()
    yield
    await _purge()


def _worker(subject: str, durable: str, *, pull: bool):
    class Worker(CliffracerService):
        db_up = True
        handled: list[int]

        @dependency("db", timeout=1.0)
        async def _check_db(self) -> None:
            if not self.db_up:
                raise ConnectionError("db refused the connection")

        @listener(subject, durable=durable, pull=pull, pause_when_down=("db",))
        async def on_item(self, subject: str, seq: int = 0) -> None:
            self.handled.append(seq)

    return Worker


async def _consumer(js, durable: str):
    return await js.consumer_info(prefixed_name(STREAM), prefixed_name(durable))


@pytest.mark.parametrize("pull", [False, True], ids=["push", "pull"])
async def test_a_paused_listener_is_handed_nothing_and_each_message_once_on_resume(pull):
    kind = "pull" if pull else "push"
    subject, durable = f"itest.pause.{kind}", f"pause-{kind}"
    worker = _worker(subject, durable, pull=pull)(_config(f"pause_worker_{kind}"))
    worker.handled = []
    publisher = CliffracerService(_config(f"pause_pub_{kind}"))
    probe_nc = await nats.connect(broker_url())
    js = probe_nc.jetstream()
    await worker.start()
    await publisher.start()
    try:
        await publisher.publish_event(subject, seq=0)
        await wait_until(lambda: worker.handled == [0], within=10, reason="the first message")

        worker.db_up = False
        pauses = worker.container.listener_pauses
        assert pauses is not None
        # The subject the listener is subscribed under, with the suite's isolation prefix.
        (effective,) = pauses.listeners
        assert effective.endswith(subject), effective
        await wait_until(
            lambda: effective in pauses.paused, within=10, reason="the listener to pause"
        )
        health = await worker.health_check()
        assert list(health["paused_listeners"]) == [effective], health
        before = await _consumer(js, durable)

        for seq in range(1, MESSAGES + 1):
            await publisher.publish_event(subject, seq=seq)
        await asyncio.sleep(ACK_WAIT * 3)

        assert worker.handled == [0], "a message reached the listener while it was paused"
        away = await _consumer(js, durable)
        assert away.num_pending == MESSAGES, away
        assert away.num_ack_pending == 0, away
        assert away.num_redelivered == 0, away
        assert away.delivered.consumer_seq == before.delivered.consumer_seq, (before, away)

        worker.db_up = True
        await wait_until(
            lambda: len(worker.handled) == MESSAGES + 1,
            within=10 + ACK_WAIT * 3,
            reason="every message once the listener resumed",
        )
        await asyncio.sleep(ACK_WAIT * 2)
        assert sorted(worker.handled) == list(range(MESSAGES + 1)), worker.handled
        after = await _consumer(js, durable)
        assert after.num_redelivered == 0, after
        assert pauses.paused == {}
    finally:
        await publisher.stop()
        await worker.stop()
        await probe_nc.close()


async def test_CONTROL_without_pause_when_down_a_down_dependency_spends_attempts():
    """The same outage with no `pause_when_down`: deliveries go on and fail, so attempts are
    spent. Without this the row above could pass because nothing was ever published."""
    subject, durable = "itest.pause.control", "pause-control"

    class Worker(CliffracerService):
        db_up = True

        @dependency("db", timeout=1.0)
        async def _check_db(self) -> None:
            if not self.db_up:
                raise ConnectionError("db refused the connection")

        @listener(subject, durable=durable)
        async def on_item(self, subject: str, seq: int = 0) -> None:
            if not self.db_up:
                raise ConnectionError("db refused the connection")

    worker = Worker(_config("pause_control_worker"))
    publisher = CliffracerService(_config("pause_control_pub"))
    probe_nc = await nats.connect(broker_url())
    js = probe_nc.jetstream()
    await worker.start()
    await publisher.start()
    try:
        worker.db_up = False
        await publisher.publish_event(subject, seq=1)

        async def redelivered() -> int:
            return (await _consumer(js, durable)).num_redelivered

        for _ in range(int(ACK_WAIT * 8 / 0.2)):
            if await redelivered() > 0:
                break
            await asyncio.sleep(0.2)
        assert await redelivered() > 0, "the failing listener was never redelivered to"
        assert worker.container.listener_pauses is None
    finally:
        await publisher.stop()
        await worker.stop()
        await probe_nc.close()


async def test_a_replica_paused_alone_leaves_the_work_to_the_other_and_the_group_unchanged():
    """Two replicas share one push durable, bound as a deliver group. One loses its dependency
    and pauses; the other keeps consuming. Everything published meanwhile goes to the replica
    still bound, once each, and the durable's deliver group is the same before the pause, during
    it and after the paused replica binds again: a rebind that dropped the group would leave the
    two replicas each taking every message, or one of them refused."""
    subject, durable = "itest.pause.pair", "pause-pair"
    worker_class = _worker(subject, durable, pull=False)
    paused = worker_class(_config("pause_pair_worker"))
    running = worker_class(_config("pause_pair_worker"))
    paused.handled, running.handled = [], []
    publisher = CliffracerService(_config("pause_pair_pub"))
    probe_nc = await nats.connect(broker_url())
    js = probe_nc.jetstream()
    await paused.start()
    await running.start()
    await publisher.start()
    try:
        group = (await _consumer(js, durable)).config.deliver_group
        assert group == prefixed_name(durable), group

        paused.db_up = False
        pauses = paused.container.listener_pauses
        assert pauses is not None
        await wait_until(lambda: pauses.paused, within=10, reason="the one replica to pause")
        assert running.container.listener_pauses.paused == {}
        assert (await _consumer(js, durable)).config.deliver_group == group

        for seq in range(MESSAGES * 2):
            await publisher.publish_event(subject, seq=seq)
        await wait_until(
            lambda: len(running.handled) == MESSAGES * 2,
            within=10,
            reason="the running replica to take everything published",
        )
        await asyncio.sleep(ACK_WAIT * 2)
        assert paused.handled == [], "the paused replica was handed a message"
        assert sorted(running.handled) == list(range(MESSAGES * 2)), running.handled

        paused.db_up = True
        await wait_until(lambda: pauses.paused == {}, within=10, reason="the replica to resume")
        after = await _consumer(js, durable)
        assert after.config.deliver_group == group, after.config
        assert after.num_redelivered == 0, after
    finally:
        await publisher.stop()
        await paused.stop()
        await running.stop()
        await probe_nc.close()
