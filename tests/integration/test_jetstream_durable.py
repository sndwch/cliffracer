"""End-to-end JetStream: durability across a restart, redelivery, and the DLQ.

Runs against a throwaway local broker only. Never point these at the shared
fleet broker.
"""

import asyncio
import json

import pytest
from cliffracer_resilience import ResilienceExtension, rate_limit
from nats.js.api import AckPolicy
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, listener, validated_listener
from cliffracer.core.discovery import HandlerDiscovery
from tests.broker_isolation import prefixed_name
from tests.conftest import broker_url

pytestmark = pytest.mark.integration


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
                await js.delete_stream(prefixed_name(name))
            except Exception:
                pass
        await nc.close()

    await _purge()
    yield
    await _purge()


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

    # Non-default tuning, so that what the server holds can only have come from this service's
    # configuration: the defaults would be read back whether or not they were ever sent.
    svc = Subscriber(
        _config(
            "itest_sub",
            jetstream_ack_wait=7.0,
            jetstream_max_deliver=4,
            jetstream_max_ack_pending=11,
        )
    )
    await svc.start()
    await asyncio.sleep(0.2)
    await svc.stop()

    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    try:
        durable = prefixed_name("itest-survivor")
        # Looked up by name, so a consumer that stop() deleted raises NotFoundError here; the
        # returned name cannot disagree with the one asked for, so it is not what is asserted.
        info = await js.consumer_info(prefixed_name("ITEST"), durable)
        assert info.config.ack_policy == AckPolicy.EXPLICIT
        assert info.config.ack_wait == 7.0
        assert info.config.max_deliver == 4
        assert info.config.max_ack_pending == 11
    finally:
        await nc.close()


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

        await svc.nc.subscribe(HandlerDiscovery.dlq_subject(svc.config), cb=_dlq_cb)
        await asyncio.sleep(0.1)

        await svc.publish_event("itest.events.boom", seq=1)

        for _ in range(100):
            if dlq:
                break
            await asyncio.sleep(0.1)

        # Exactly the configured number, not at least: `>=` is satisfied by a budget blown to
        # nine attempts, which is the load every poison message would then put on the handler.
        assert len(attempts) == 3, f"expected exactly 3 deliveries, saw {len(attempts)}"
        assert dlq, "message never reached the DLQ subject"
        assert dlq[0]["original_subject"] == HandlerDiscovery.with_namespace(
            svc.config, "itest.events.boom"
        )
        assert dlq[0]["deliveries"] == 3

        # Dead-lettered means finished: past the ack wait and the longest backoff, the handler
        # has not been called again and nothing else reached the DLQ.
        await asyncio.sleep(cfg.jetstream_ack_wait * 2 + cfg.jetstream_max_backoff)
        assert len(attempts) == 3, f"redelivered after the DLQ: {len(attempts)} attempts"
        assert len(dlq) == 1, f"dead-lettered more than once: {len(dlq)}"
    finally:
        await svc.stop()


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_the_server_side_consumer_carries_the_configured_tuning():
    """Read the durable back from the broker: the tuning is what the SERVER enforces.

    The redelivery count above shows `max_deliver` end to end; this is the one place that reads
    the consumer the service created, so `ack_wait`, `max_ack_pending` and the ack policy are
    seen where they decide and not only through their effects.
    """

    class Tuned(CliffracerService):
        @listener("itest.events.tuned", durable="itest-tuned")
        async def on_tuned(self, subject: str) -> None:
            pass

    svc = Tuned(
        _config(
            "itest_tuned",
            jetstream_max_deliver=4,
            jetstream_ack_wait=2.5,
            jetstream_max_ack_pending=7,
        )
    )
    await svc.start()
    try:
        info = await svc.js.consumer_info(prefixed_name("ITEST"), prefixed_name("itest-tuned"))
    finally:
        await svc.stop()

    assert info.config.max_deliver == 4
    assert info.config.ack_wait == 2.5
    assert info.config.max_ack_pending == 7
    assert info.config.ack_policy == AckPolicy.EXPLICIT


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_durable_work_waits_for_rate_limit_capacity_and_then_runs():
    processed: list[str] = []

    class LimitedOrders(CliffracerService):
        resilience = ResilienceExtension()

        @listener("itest.events.limited", durable="itest-limited-orders")
        @rate_limit(calls=1, window=0.25)
        async def on_order(self, order_id: str) -> None:
            processed.append(order_id)

    svc = LimitedOrders(
        _config(
            "itest_limited",
            jetstream_max_deliver=3,
            jetstream_nak_backoff=0.05,
            jetstream_max_backoff=0.1,
            jetstream_ack_wait=1.0,
        )
    )
    await svc.start()
    try:
        await svc.publish_event("itest.events.limited", order_id="order-1")
        for _ in range(50):
            if processed:
                break
            await asyncio.sleep(0.01)
        assert processed == ["order-1"]

        loop = asyncio.get_running_loop()
        published_at = loop.time()
        await svc.publish_event("itest.events.limited", order_id="order-2")
        for _ in range(100):
            if len(processed) == 2:
                break
            await asyncio.sleep(0.01)

        assert processed == ["order-1", "order-2"]
        assert loop.time() - published_at >= 0.15
    finally:
        await svc.stop()


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

        await svc.nc.subscribe(HandlerDiscovery.dlq_subject(svc.config), cb=_dlq_cb)
        await asyncio.sleep(0.1)

        await svc.publish_event("events.ping", seq="not-a-number")

        for _ in range(50):
            if dlq:
                break
            await asyncio.sleep(0.1)

        assert dlq, "invalid message never reached the namespaced DLQ subject"
        assert dlq[0]["original_subject"] == HandlerDiscovery.with_namespace(
            svc.config, "events.ping"
        )
        # the body that failed validation, and why
        assert dlq[0]["schema"] == "Ping", dlq[0]
        assert dlq[0]["payload"]["data"]["seq"] == "not-a-number", dlq[0]
        assert [tuple(e["loc"]) for e in dlq[0]["errors"]] == [("seq",)], dlq[0]
    finally:
        await svc.stop()


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
    # Both starts are inside the try: a second start that raises must still stop the first.
    try:
        await a.start()
        await b.start()
        await asyncio.sleep(0.2)
        await a.publish_event("itest.events.shared", seq=1)
        await asyncio.sleep(1.0)
        assert len(handled) == 1, f"expected one delivery across the pair, got {handled}"
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_an_identical_stream_declaration_from_two_services_is_a_no_op(monkeypatch):
    """A publisher and its consumer both declare the shared stream.

    "No-op" is read from the server: the second service's start issues no `add_stream` and no
    `update_stream` (a rewrite to the configuration the stream already has does not raise, and is
    the boot-time churn the design exists to prevent), and the stream is unchanged.
    """
    from nats.js import JetStreamContext

    a = CliffracerService(_config("itest_a"))
    b = CliffracerService(_config("itest_b"))
    await a.start()
    try:
        before = await a.container.connection.js.stream_info(prefixed_name("ITEST"))

        calls: list[str] = []
        for method in ("add_stream", "update_stream"):
            real = getattr(JetStreamContext, method)

            def recording(self, *args, _real=real, _name=method, **kwargs):
                calls.append(_name)
                return _real(self, *args, **kwargs)

            monkeypatch.setattr(JetStreamContext, method, recording)

        await b.start()  # must not raise
        await b.stop()

        after = await a.container.connection.js.stream_info(prefixed_name("ITEST"))
        assert calls == [], f"the second service rewrote the shared stream: {calls}"
        assert after.config == before.config
        assert after.state.first_seq == before.state.first_seq
    finally:
        await a.stop()


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
