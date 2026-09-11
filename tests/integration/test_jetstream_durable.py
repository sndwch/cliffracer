"""End-to-end JetStream: durability across a restart, redelivery, and the DLQ.

Runs against a throwaway local broker only. Never point these at the shared
fleet broker.
"""

import asyncio
import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, listener, validated_listener
from tests.conftest import broker_url


class Ping(BaseModel):
    seq: int


def _config(name, **overrides):
    return ServiceConfig(
        name=name,
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="ITEST", subjects=["itest.events.*"]),
            StreamSpec(name="ITEST_DLQ", subjects=["dlq.*"]),
        ],
        **overrides,
    )


@pytest.fixture(autouse=True)
async def _clean_streams():
    """Delete the test streams before and after, so runs do not leak into each other."""
    import nats

    async def _purge():
        # broker_url(), not the ServiceConfig default: these two are RAW
        # nats.connect calls, so the conftest's default-override cannot
        # reach them even in principle.
        nc = await nats.connect(broker_url())
        js = nc.jetstream()
        for name in ("ITEST", "ITEST_DLQ", "NSTEST", "NSTEST_DLQ"):
            try:
                await js.delete_stream(name)
            except Exception:
                pass
        await nc.close()

    await _purge()
    yield
    await _purge()


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_event_published_while_subscriber_is_down_is_delivered_on_restart():
    """Verify messages published while subscriber is offline are delivered on restart."""
    received: list = []

    class Subscriber(CliffracerService):
        @listener("itest.events.ping", durable="itest-pinger")
        async def on_ping(self, subject: str, seq: int = 1) -> None:
            received.append({"seq": seq})

    # Start once so the durable consumer exists, then stop it.
    sub = Subscriber(_config("itest_sub"))
    await sub.start()
    await asyncio.sleep(0.2)
    await sub.stop()

    # Publish while nothing is listening.
    publisher = CliffracerService(_config("itest_pub"))
    await publisher.start()
    await publisher.publish_event("itest.events.ping", seq=1)
    await publisher.stop()

    # Restart the subscriber: the message must arrive.
    sub2 = Subscriber(_config("itest_sub"))
    await sub2.start()
    try:
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.1)
        assert received, "durable subscriber did not receive the event published while it was down"
        assert received[0]["seq"] == 1
    finally:
        await sub2.stop()


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_the_durable_consumer_survives_a_service_stop():
    """If stop() deleted the consumer, the test above would still pass in-process
    while redelivery-after-restart silently stopped working in production."""
    import nats

    class Subscriber(CliffracerService):
        @listener("itest.events.ping", durable="itest-survivor")
        async def on_ping(self, subject: str) -> None:
            pass

    svc = Subscriber(_config("itest_sub"))
    await svc.start()
    await asyncio.sleep(0.2)
    await svc.stop()

    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    try:
        info = await js.consumer_info("ITEST", "itest-survivor")
        assert info.name == "itest-survivor"
    finally:
        await nc.close()


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_a_raising_handler_redelivers_then_lands_on_the_dlq():
    attempts: list = []
    dlq: list = []

    class Failing(CliffracerService):
        @listener("itest.events.boom", durable="itest-boom")
        async def on_boom(self, subject: str, seq: int = 1) -> None:
            attempts.append({"seq": seq})
            raise RuntimeError("always fails")

    cfg = _config(
        "itest_boom",
        jetstream_max_deliver=3,
        jetstream_nak_backoff=0.1,
        jetstream_max_backoff=0.2,
        jetstream_ack_wait=1.0,
    )
    svc = Failing(cfg)
    await svc.start()
    try:

        async def _dlq_cb(msg):
            dlq.append(json.loads(msg.data.decode()))

        await svc.nc.subscribe("dlq.itest_boom", cb=_dlq_cb)
        await asyncio.sleep(0.1)

        await svc.publish_event("itest.events.boom", seq=1)

        for _ in range(100):
            if dlq:
                break
            await asyncio.sleep(0.1)

        assert len(attempts) >= 3, f"expected at least 3 deliveries, saw {len(attempts)}"
        assert dlq, "message never reached the DLQ subject"
        assert dlq[0]["original_subject"] == "itest.events.boom"
        assert dlq[0]["deliveries"] >= 3
    finally:
        await svc.stop()


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_a_namespaced_service_dead_letters_an_invalid_message_for_real():
    """Proves the claim, the startup assertion and the runtime subject agree.

    A namespace-less version of this test would pass against the bug, because
    'dlq.*' does match 'dlq.{service}' when no namespace is set.
    """
    dlq: list = []

    class Listener(CliffracerService):
        @validated_listener("events.ping", Ping, durable="ns-pinger")
        async def on_ping(self, message: Ping):
            pass

    cfg = ServiceConfig(
        name="ns_svc",
        namespace="utils",
        dlq_subject="{namespace}.dlq.{service}",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="NSTEST", subjects=["utils.events.*"]),
            StreamSpec(name="NSTEST_DLQ", subjects=["utils.dlq.*"]),
        ],
    )
    svc = Listener(cfg)
    await svc.start()
    try:

        async def _dlq_cb(msg):
            dlq.append(json.loads(msg.data.decode()))

        await svc.nc.subscribe("utils.dlq.ns_svc", cb=_dlq_cb)
        await asyncio.sleep(0.1)

        await svc.publish_event("events.ping", seq="not-a-number")

        for _ in range(50):
            if dlq:
                break
            await asyncio.sleep(0.1)

        assert dlq, "invalid message never reached the namespaced DLQ subject"
        assert dlq[0]["original_subject"] == "utils.events.ping"
    finally:
        await svc.stop()


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_two_replicas_share_one_durable_consumer():
    """Two services with the same durable name must both bind, and a message
    must be handled once across the pair rather than twice."""
    handled: list = []

    def _make(instance):
        class Replica(CliffracerService):
            @listener("itest.events.shared", durable="itest-shared")
            async def on_event(self, subject: str, seq: int = 1) -> None:
                handled.append((instance, {"seq": seq}))

        return Replica(_config("itest_replica"))

    a, b = _make("a"), _make("b")
    await a.start()
    await b.start()
    try:
        await asyncio.sleep(0.2)
        await a.publish_event("itest.events.shared", seq=1)
        await asyncio.sleep(1.0)
        assert len(handled) == 1, f"expected one delivery across the pair, got {handled}"
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_an_identical_stream_declaration_from_two_services_is_a_no_op():
    """A publisher and its consumer both declare the shared stream."""
    a = CliffracerService(_config("itest_a"))
    b = CliffracerService(_config("itest_b"))
    await a.start()
    try:
        await b.start()  # must not raise
        await b.stop()
    finally:
        await a.stop()


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_a_conflicting_stream_declaration_fails_startup_with_a_named_error():
    from cliffracer.core.jetstream import StreamDeclarationError

    a = CliffracerService(_config("itest_a"))
    await a.start()
    try:
        clashing = ServiceConfig(
            name="itest_clash",
            jetstream_enabled=True,
            jetstream_streams=[
                StreamSpec(name="OTHER", subjects=["itest.events.>"]),
                StreamSpec(name="ITEST_DLQ2", subjects=["dlq.itest_clash"]),
            ],
        )
        # Retain reference to service instance so resources can be stopped in finally block.
        clash = CliffracerService(clashing)
        try:
            with pytest.raises(StreamDeclarationError) as exc:
                await clash.start()
            assert "ITEST" in str(exc.value)
            assert "OTHER" in str(exc.value)
            assert "itest.events.>" in str(exc.value)
        finally:
            # Ensure resources are released if start() failed.
            await clash.stop()
    finally:
        await a.stop()
