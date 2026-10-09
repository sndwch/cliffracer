"""Adversarial integration suite for live broker concurrency and dead-letter routing."""

import asyncio
import json

import nats
import pytest
from pydantic import BaseModel

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    StreamSpec,
    listener,
    rpc,
    validated_listener,
)
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.testing.waiting import wait_until
from tests.broker_isolation import prefixed_name
from tests.conftest import broker_url

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _clean_test_streams():
    """Purge any test streams before and after."""
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    for name in ("CHALLENGE_EVENTS", "CHALLENGE_DLQ"):
        try:
            await js.delete_stream(prefixed_name(name))
        except Exception:
            pass
    await nc.close()
    yield
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    for name in ("CHALLENGE_EVENTS", "CHALLENGE_DLQ"):
        try:
            await js.delete_stream(prefixed_name(name))
        except Exception:
            pass
    await nc.close()


class SimpleResult(BaseModel):
    req_id: int
    active_during: int


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_bounded_rpc_concurrency():
    """Over live NATS: 20 concurrent requests against a service with max_rpc_concurrency=2.

    Verifies that in-flight execution at no point exceeds 2, all 20 requests
    receive correct replies, and the semaphore does not leak.
    """
    in_flight = 0
    max_observed = 0
    lock = asyncio.Lock()

    class LiveWorker(CliffracerService):
        @rpc
        async def compute(self, req_id: int) -> SimpleResult:
            nonlocal in_flight, max_observed
            async with lock:
                in_flight += 1
                if in_flight > max_observed:
                    max_observed = in_flight

            await asyncio.sleep(0.04)

            async with lock:
                active_now = in_flight
                in_flight -= 1
            return SimpleResult(req_id=req_id, active_during=active_now)

    cfg = ServiceConfig(name="live_bounded_svc", max_rpc_concurrency=2)
    svc = LiveWorker(cfg)
    await svc.start()

    client_nc = await nats.connect(broker_url())
    try:

        async def call_rpc(i: int):
            payload = json.dumps({"req_id": i}).encode()
            resp = await client_nc.request(
                HandlerDiscovery.outbound_subject(cfg, "live_bounded_svc", "rpc", "compute"),
                payload,
                timeout=10.0,
                headers={"Content-Type": "application/json"},
            )
            return json.loads(resp.data.decode())

        results = await asyncio.gather(*[call_rpc(i) for i in range(20)])

        assert max_observed <= 2, f"Observed concurrency {max_observed} exceeded limit 2"
        for i, res in enumerate(results):
            assert res["success"] is True
            assert res["result"]["req_id"] == i
    finally:
        await client_nc.close()
        await svc.stop()


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_shutdown_deadline_cancels_hung_handler():
    """Over live NATS: service shutdown cancels hung in-flight handler within shutdown_timeout."""

    cancelled = False

    class HungLiveWorker(CliffracerService):
        @rpc
        async def slow_rpc(self) -> str:
            nonlocal cancelled
            try:
                await asyncio.sleep(100.0)
            except asyncio.CancelledError:
                cancelled = True
                raise
            return "done"

    cfg = ServiceConfig(name="hung_live_svc", shutdown_timeout=0.3)
    svc = HungLiveWorker(cfg)
    await svc.start()

    client_nc = await nats.connect(broker_url())
    client_task = None
    try:
        # Fire request asynchronously without waiting for response (since it hangs)
        client_task = asyncio.create_task(
            client_nc.request(
                HandlerDiscovery.outbound_subject(cfg, "hung_live_svc", "rpc", "slow_rpc"),
                b"{}",
                timeout=5.0,
            )
        )
        await asyncio.sleep(0.1)

        # Service now has 1 active task
        assert len(svc.container._active_tasks) == 1

        await svc.stop()

        # The handler sleeps 100s, so cancellation is the only way it ends:
        # the except branch says shutdown reached it over the live broker.
        assert cancelled is True
        assert len(svc.container._active_tasks) == 0

        # Client request should time out or get no responders
        with pytest.raises((TimeoutError, nats.errors.Error, asyncio.TimeoutError)):
            await client_task
    finally:
        # The stop above is in the body, behind assertions: a failure there must still stop the
        # service (a second stop is a no-op), or the leaked-task guard reports its own error first.
        if client_task is not None and not client_task.done():
            client_task.cancel()
            await asyncio.gather(client_task, return_exceptions=True)
        await svc.stop()
        await client_nc.close()


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_complex_overlapping_listeners():
    """Over live NATS: complex multi-level overlapping patterns:
    - `*.*`
    - `orders.*`
    - `orders.created`
    - `orders.cancelled`

    Verifies that publishing an event executes each matching listener EXACTLY ONCE
    and does not duplicate executions across subscriptions.
    """
    executions = {
        "wildcard_all": [],
        "orders_wildcard": [],
        "orders_created": [],
        "orders_cancelled": [],
    }

    class MultiListenerService(CliffracerService):
        @listener("*.*", fanout=True)
        async def on_wildcard_all(
            self,
            subject: str,
            order_id: str | None = None,
            tx: str | None = None,
        ) -> None:
            data = {"order_id": order_id} if order_id is not None else {"tx": tx}
            executions["wildcard_all"].append((subject, data))

        @listener("orders.*", fanout=True)
        async def on_orders_wildcard(self, subject: str, order_id: str = "") -> None:
            executions["orders_wildcard"].append((subject, {"order_id": order_id}))

        @listener("orders.created", fanout=True)
        async def on_orders_created(self, subject: str, order_id: str = "") -> None:
            executions["orders_created"].append((subject, {"order_id": order_id}))

        @listener("orders.cancelled", fanout=True)
        async def on_orders_cancelled(self, subject: str, order_id: str = "") -> None:
            executions["orders_cancelled"].append((subject, {"order_id": order_id}))

    cfg = ServiceConfig(name="live_overlapping_svc")
    svc = MultiListenerService(cfg)
    await svc.start()

    created = HandlerDiscovery.with_namespace(cfg, "orders.created")
    cancelled = HandlerDiscovery.with_namespace(cfg, "orders.cancelled")
    refunded = HandlerDiscovery.with_namespace(cfg, "payments.refunded")

    async def delivered(*, wildcard_all: int, orders_wildcard: int, per_name: int) -> None:
        """Wait for the deliveries this step is expected to produce, then for the broker to
        have nothing further in flight, so the exact contents asserted next are the final ones."""
        await wait_until(
            lambda: (
                len(executions["wildcard_all"]) >= wildcard_all
                and len(executions["orders_wildcard"]) >= orders_wildcard
                and len(executions["orders_created"]) + len(executions["orders_cancelled"])
                >= per_name
            ),
            within=10.0,
            reason="every matching listener to receive the event",
        )
        await svc.nc.flush()

    try:
        # Publish 1: orders.created
        await svc.publish_event("orders.created", order_id="ord-01")
        await delivered(wildcard_all=1, orders_wildcard=1, per_name=1)

        assert executions == {
            "wildcard_all": [(created, {"order_id": "ord-01"})],
            "orders_wildcard": [(created, {"order_id": "ord-01"})],
            "orders_created": [(created, {"order_id": "ord-01"})],
            "orders_cancelled": [],
        }

        # Publish 2: orders.cancelled
        await svc.publish_event("orders.cancelled", order_id="ord-02")
        await delivered(wildcard_all=2, orders_wildcard=2, per_name=2)

        assert executions == {
            "wildcard_all": [
                (created, {"order_id": "ord-01"}),
                (cancelled, {"order_id": "ord-02"}),
            ],
            "orders_wildcard": [
                (created, {"order_id": "ord-01"}),
                (cancelled, {"order_id": "ord-02"}),
            ],
            "orders_created": [(created, {"order_id": "ord-01"})],
            "orders_cancelled": [(cancelled, {"order_id": "ord-02"})],
        }

        # Publish 3: payments.refunded (matches *.* only)
        await svc.publish_event("payments.refunded", tx="tx-01")
        await delivered(wildcard_all=3, orders_wildcard=2, per_name=2)

        # The `*.*` handler saw the refund, with its own payload and subject, and nothing
        # else heard it: the three `orders` buckets are unchanged from the step before.
        assert executions["wildcard_all"][2] == (refunded, {"tx": "tx-01"})
        assert len(executions["wildcard_all"]) == 3
        assert executions["orders_wildcard"] == [
            (created, {"order_id": "ord-01"}),
            (cancelled, {"order_id": "ord-02"}),
        ]
        assert executions["orders_created"] == [(created, {"order_id": "ord-01"})]
        assert executions["orders_cancelled"] == [(cancelled, {"order_id": "ord-02"})]
    finally:
        await svc.stop()


class ValidatedItem(BaseModel):
    sku: str
    quantity: int


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_jetstream_durable_adversarial_payloads_dlq_and_termination():
    """Over live JetStream: adversarial payloads sent to durable listener:
    1. Completely invalid bytes (b"\\x80\\xff")
    2. Truncated malformed JSON (b'{"sku": "ABC", "quantity": ')
    3. Empty payload (b"")
    4. Wrong field data type (b'{"sku": "ABC", "quantity": "twenty"}')
    5. Missing required field (b'{"sku": "ABC"}')

    Verifies:
    - All 5 reach the DLQ subject dlq.live_durable_svc
    - None hang or loop infinitely
    - The JetStream durable consumer does NOT have pending messages after processing
    """
    dlq_received = []

    class DurableConsumerService(CliffracerService):
        @validated_listener("challenge.events.order", ValidatedItem, durable="live-order-proc")
        async def on_order(self, message: ValidatedItem):
            pass

    cfg = ServiceConfig(
        name="live_durable_svc",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="CHALLENGE_EVENTS", subjects=["challenge.events.*"]),
            StreamSpec(name="CHALLENGE_DLQ", subjects=["dlq.live_durable_svc"]),
        ],
    )
    svc = DurableConsumerService(cfg)
    await svc.start()

    raw_nc = await nats.connect(broker_url())
    raw_js = raw_nc.jetstream()

    try:
        # Subscribe to DLQ to capture dead-lettered events
        async def on_dlq(msg):
            data = json.loads(msg.data.decode(errors="replace"))
            dlq_received.append(data)

        await raw_nc.subscribe(HandlerDiscovery.dlq_subject(cfg), cb=on_dlq)
        await raw_nc.flush()

        adversarial_payloads = [
            b"\x80\xff",
            b'{"sku": "ABC", "quantity": ',
            b"",
            b'{"sku": "ABC", "quantity": "twenty"}',
            b'{"sku": "ABC"}',
        ]

        # Publish all 5 directly to the JetStream stream
        for p in adversarial_payloads:
            await raw_js.publish(HandlerDiscovery.with_namespace(cfg, "challenge.events.order"), p)

        # Wait for durable service to process and dead-letter all 5
        for _ in range(60):
            if len(dlq_received) == 5:
                break
            await asyncio.sleep(0.1)

        assert len(dlq_received) == 5, (
            f"Expected 5 DLQ messages, got {len(dlq_received)}: {dlq_received}"
        )

        # What each dead letter CARRIES, not only that five arrived: five copies of one poison
        # message, or envelopes that threw the offending bytes away, also count five.
        undecodable = [d for d in dlq_received if "errors" not in d]
        assert sorted(d["payload"]["raw"] for d in undecodable) == sorted(
            [adversarial_payloads[0].decode(errors="replace"), adversarial_payloads[1].decode()]
        ), undecodable
        assert all(d["error"].startswith("Decode error") for d in undecodable), undecodable

        invalid = {
            json.dumps(d["payload"], sort_keys=True): sorted(
                (tuple(e["loc"]), e["type"]) for e in d["errors"]
            )
            for d in dlq_received
            if "errors" in d
        }
        assert invalid == {
            "{}": [(("quantity",), "missing"), (("sku",), "missing")],  # the empty payload
            '{"quantity": "twenty", "sku": "ABC"}': [(("quantity",), "int_parsing")],
            '{"sku": "ABC"}': [(("quantity",), "missing")],
        }, invalid
        assert {d["schema"] for d in dlq_received if "errors" in d} == {"ValidatedItem"}

        # Verify consumer state: no pending, no ack_pending, no infinite redelivery loops
        consumer_info = await raw_js.consumer_info(
            prefixed_name("CHALLENGE_EVENTS"), prefixed_name("live-order-proc")
        )
        assert consumer_info.num_pending == 0, (
            f"Pending messages remaining: {consumer_info.num_pending}"
        )
        assert consumer_info.num_ack_pending == 0, (
            f"Ack pending remaining: {consumer_info.num_ack_pending}"
        )

    finally:
        await raw_nc.close()
        await svc.stop()
