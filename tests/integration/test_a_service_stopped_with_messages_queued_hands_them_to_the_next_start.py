"""A service stopped while messages wait for a concurrency permit starts none of them, and each is handled once.

`max_event_concurrency=1` with a slow handler leaves most of a burst waiting for the permit. A service
that is stopping finishes the handler it has running and starts no message that waited: the message is
left unacknowledged, and the broker redelivers it once its `ack_wait` passes, to the next start of the
service. Across the stop and the restart every message is handled exactly once: none lost with the
stop, none run twice.

Runs against a throwaway local broker only.
"""

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, listener
from tests.broker_isolation import prefixed_name
from tests.conftest import broker_url

pytestmark = pytest.mark.integration

MESSAGES = 8
ACK_WAIT = 1.0
HANDLER_SECONDS = 0.4


def _config(name):
    return ServiceConfig(
        name=name,
        jetstream_enabled=True,
        jetstream_ack_wait=ACK_WAIT,
        max_event_concurrency=1,
        jetstream_streams=[
            StreamSpec(name="WAITQ", subjects=["itest.waitq.*"]),
            StreamSpec(name="WAITQ_DLQ", subjects=["dlq.*"]),
        ],
    )


@pytest.fixture(autouse=True)
async def _clean_streams():
    import nats

    async def _purge():
        nc = await nats.connect(broker_url())
        js = nc.jetstream()
        for name in ("WAITQ", "WAITQ_DLQ"):
            try:
                await js.delete_stream(prefixed_name(name))
            except Exception:
                pass
        await nc.close()

    await _purge()
    yield
    await _purge()


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_a_stop_with_messages_queued_and_a_restart_handle_each_message_once():
    handled: list[int] = []

    class Worker(CliffracerService):
        @listener("itest.waitq.item", durable="waitq-worker")
        async def on_item(self, subject: str, seq: int = 0) -> None:
            await asyncio.sleep(HANDLER_SECONDS)
            handled.append(seq)

    worker = Worker(_config("waitq_worker"))
    await worker.start()
    publisher = CliffracerService(_config("waitq_pub"))
    await publisher.start()
    for seq in range(MESSAGES):
        await publisher.publish_event("itest.waitq.item", seq=seq)
    await publisher.stop()

    await asyncio.sleep(0.6)  # the first handler is done or running, the rest wait for the permit
    await worker.stop()
    handled_before_the_stop = list(handled)

    assert handled_before_the_stop, "nothing ran before the stop, so nothing was in flight"
    assert len(handled_before_the_stop) < MESSAGES, "the whole burst ran before the stop"

    restarted = Worker(_config("waitq_worker"))
    await restarted.start()
    try:
        for _ in range(int((ACK_WAIT * 4 + MESSAGES * HANDLER_SECONDS) / 0.1)):
            if len(handled) >= MESSAGES:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(ACK_WAIT * 1.5)  # a second run of any message would show by now
    finally:
        await restarted.stop()

    assert sorted(handled) == list(range(MESSAGES)), (
        f"handled {sorted(handled)}; before the stop {handled_before_the_stop}"
    )
