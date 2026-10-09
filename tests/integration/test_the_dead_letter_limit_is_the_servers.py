"""A drifted durable still dead-letters, at the limit the server enforces.

A durable consumer's config is fixed when it is created, and a later subscribe
adopts it rather than updating it. A service restarted with a higher
`jetstream_max_deliver` therefore runs against the old limit: the server stops
redelivering there, and a dead-letter decision taken against the new, local
number is never reached. The poison message stops arriving and is recorded
nowhere.

Both halves are asserted against a real broker, because the adoption is a
property of the client and the server rather than of this code: the durable
keeps the limit it was created with, AND the message that exhausts it is
dead-lettered.

Runs against a throwaway local broker only. Never point these at the shared
fleet broker.
"""

import asyncio
import json

import nats
import pytest

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, listener
from cliffracer.core.discovery import HandlerDiscovery
from tests.broker_isolation import prefixed_name
from tests.conftest import broker_url

pytestmark = pytest.mark.integration

STREAMS = ("MDTEST", "MDTEST_DLQ")

# Long enough for a third delivery to arrive if the server were going to make
# one: the nak backoff below is at most 0.2s and the ack wait 1s.
SETTLE_SECONDS = 3.0


def _config(max_deliver: int) -> ServiceConfig:
    return ServiceConfig(
        name="mdtest",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="MDTEST", subjects=["mdtest.events.*"]),
            StreamSpec(name="MDTEST_DLQ", subjects=["mdtest_dlq.*"]),
        ],
        dlq_subject="mdtest_dlq.{service}",
        jetstream_max_deliver=max_deliver,
        jetstream_nak_backoff=0.1,
        jetstream_max_backoff=0.2,
        jetstream_ack_wait=1.0,
    )


@pytest.fixture(autouse=True)
async def _clean_streams():
    async def _purge():
        nc = await nats.connect(broker_url())
        js = nc.jetstream()
        for name in STREAMS:
            try:
                await js.delete_stream(prefixed_name(name))
            except Exception:
                pass
        await nc.close()

    await _purge()
    yield
    await _purge()


def _service_class(durable: str, pull: bool, attempts: list):
    class Failing(CliffracerService):
        @listener("mdtest.events.boom", durable=durable, pull=pull)
        async def on_boom(self, subject: str, seq: int = 1) -> None:
            attempts.append(seq)
            raise RuntimeError("always fails")

    return Failing


@pytest.mark.nats_required
@pytest.mark.asyncio
@pytest.mark.parametrize("pull", [False, True], ids=["push", "pull"])
async def test_a_durable_created_with_a_lower_limit_still_dead_letters(pull):
    durable = f"mdtest-{'pull' if pull else 'push'}"
    attempts: list = []
    dlq: list = []
    Failing = _service_class(durable, pull, attempts)

    # Create the durable at max_deliver=2, as an earlier deploy would have.
    first = Failing(_config(max_deliver=2))
    await first.start()
    await asyncio.sleep(0.2)
    await first.stop()

    # Redeploy with the limit raised to 5.
    svc = Failing(_config(max_deliver=5))
    await svc.start()
    try:
        info = await svc.js.consumer_info(prefixed_name("MDTEST"), prefixed_name(durable))
        assert info.config.max_deliver == 2, (
            f"the premise does not hold: the durable was updated to "
            f"{info.config.max_deliver} rather than keeping the limit it was created with"
        )

        async def _dlq_cb(msg):
            dlq.append(json.loads(msg.data.decode()))

        await svc.nc.subscribe(HandlerDiscovery.dlq_subject(svc.config), cb=_dlq_cb)
        await asyncio.sleep(0.1)

        await svc.publish_event("mdtest.events.boom", seq=1)

        for _ in range(int(SETTLE_SECONDS * 10)):
            if dlq:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(SETTLE_SECONDS)

        assert len(attempts) == 2, f"the server's limit is 2; saw {len(attempts)} deliveries"
        assert len(dlq) == 1, (
            f"after the server's last delivery the message was dead-lettered "
            f"{len(dlq)} times, not once"
        )
        assert dlq[0]["deliveries"] == 2
        assert dlq[0]["delivery_limit"] == "server max_deliver 2", dlq[0]
    finally:
        await svc.stop()
