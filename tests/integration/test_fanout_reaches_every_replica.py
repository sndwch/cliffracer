"""fanout=True on a real broker: every replica of one service handles every message.

Runs against a throwaway local broker only. Never point these at the shared fleet broker.

The other half of the promise, one replica per message for a durable, is
`test_jetstream_durable.py::test_two_replicas_share_one_durable_consumer`. The unit test that
reads the subscribe call's kwargs shows the listener asks for no queue group; only a broker that
is given two subscribers shows it delivers to both.
"""

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, listener

pytestmark = pytest.mark.integration

MESSAGES = 3


def _config(jetstream: bool) -> ServiceConfig:
    """Two replicas are two services with one name, so one config shape builds both."""
    if not jetstream:
        return ServiceConfig(name="itest_fan")
    return ServiceConfig(
        name="itest_fan",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="ITEST_FAN", subjects=["itest.events.*"]),
            StreamSpec(name="ITEST_FAN_DLQ", subjects=["dlq.*"]),
        ],
    )


@pytest.mark.nats_required
@pytest.mark.asyncio
@pytest.mark.parametrize("jetstream", [False, True], ids=["core", "jetstream"])
async def test_a_fanout_listener_handles_every_message_on_every_replica(jetstream):
    handled: list[tuple[str, int]] = []

    def _replica(instance: str) -> CliffracerService:
        class Replica(CliffracerService):
            @listener("itest.events.fan", fanout=True)
            async def on_fan(self, subject: str, seq: int = 0) -> None:
                handled.append((instance, seq))

        return Replica(_config(jetstream))

    a, b = _replica("a"), _replica("b")
    await a.start()
    await b.start()
    try:
        for seq in range(MESSAGES):
            await a.publish_event("itest.events.fan", seq=seq)

        async with asyncio.timeout(10):
            while len(handled) < 2 * MESSAGES:
                await asyncio.sleep(0.05)
        # Nothing arrives late: the count is the whole of what was delivered.
        await asyncio.sleep(0.5)

        assert sorted(handled) == sorted(
            (instance, seq) for instance in ("a", "b") for seq in range(MESSAGES)
        ), handled
    finally:
        await a.stop()
        await b.stop()
