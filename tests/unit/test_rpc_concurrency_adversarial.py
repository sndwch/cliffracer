"""Adversarial stress harness for concurrency bounds and malformed payloads."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

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

pytestmark = pytest.mark.unit


def _rpc_msg(
    subject: str = "svc.rpc.work",
    data: dict | None = None,
    raw_data: bytes | None = None,
    respond_side_effect=None,
):
    msg = AsyncMock()
    msg.subject = subject
    msg.reply = "reply.test.123"
    if raw_data is not None:
        msg.data = raw_data
    else:
        msg.data = json.dumps(data or {}).encode()
    msg.headers = None
    if respond_side_effect:
        msg.respond.side_effect = respond_side_effect
    return msg


# ============================================================================
# Section 1: Bounded RPC Concurrency & Shutdown Deadline
# ============================================================================


@pytest.mark.asyncio
async def test_stress_concurrent_rpc_requests_exceeding_concurrency_bound():
    """Stress test: 50 concurrent requests against a max_rpc_concurrency of 4.

    Verifies that the semaphore strictly limits concurrency to <= 4 at all times,
    and all 50 requests successfully finish with valid responses.
    """
    current_in_flight = 0
    max_observed_concurrency = 0
    lock = asyncio.Lock()

    class WorkResult(BaseModel):
        index: int
        status: str

    class ConcurrencyStressService(CliffracerService):
        @rpc
        async def do_work(self, index: int) -> WorkResult:
            nonlocal current_in_flight, max_observed_concurrency
            async with lock:
                current_in_flight += 1
                if current_in_flight > max_observed_concurrency:
                    max_observed_concurrency = current_in_flight

            # Simulate non-trivial I/O / work
            await asyncio.sleep(0.02)

            async with lock:
                current_in_flight -= 1
            return WorkResult(index=index, status="processed")

    config = ServiceConfig(name="stress_bounded_svc", max_rpc_concurrency=4)
    svc = ConcurrencyStressService(config)
    svc._discover_handlers()
    svc.container.lifecycle._running = True

    total_requests = 50
    msgs = [
        _rpc_msg("stress_bounded_svc.rpc.do_work", data={"index": i}) for i in range(total_requests)
    ]

    tasks = [asyncio.create_task(svc.container._on_rpc_request(m)) for m in msgs]
    await asyncio.gather(*tasks)
    if getattr(svc.container, "_active_tasks", None):
        await asyncio.gather(*list(svc.container._active_tasks))
    await asyncio.sleep(0)

    # Wait for all background in-flight tasks to drain
    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert max_observed_concurrency <= 4, (
        f"Observed concurrency {max_observed_concurrency} exceeded max 4"
    )
    assert current_in_flight == 0

    # Verify all requests received valid replies
    for i, m in enumerate(msgs):
        assert m.respond.await_count == 1
        reply_bytes = m.respond.call_args.args[0]
        reply = json.loads(reply_bytes.decode())
        assert reply["success"] is True
        assert reply["result"]["index"] == i
        assert reply["result"]["status"] == "processed"


@pytest.mark.asyncio
async def test_semaphore_never_leaked_on_handler_exceptions():
    """Verify that handler exceptions do NOT leak semaphore permits."""

    class FailingService(CliffracerService):
        @rpc
        async def fail_work(self) -> str:
            await asyncio.sleep(0.01)
            raise ValueError("Intentional handler failure")

    config = ServiceConfig(name="failing_svc", max_rpc_concurrency=3)
    svc = FailingService(config)
    svc._discover_handlers()
    svc.container.lifecycle._running = True

    # Initial semaphore value
    sem = svc.container._get_rpc_semaphore()
    assert sem._value == 3

    # Send 24 failing requests
    msgs = [_rpc_msg("failing_svc.rpc.fail_work") for _ in range(24)]
    tasks = [asyncio.create_task(svc.container._on_rpc_request(m)) for m in msgs]
    await asyncio.gather(*tasks)
    if getattr(svc.container, "_active_tasks", None):
        await asyncio.gather(*list(svc.container._active_tasks))
    await asyncio.sleep(0)

    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks), return_exceptions=True)

    # All permits must have been returned
    assert sem._value == 3

    for m in msgs:
        assert m.respond.await_count == 1
        reply = json.loads(m.respond.call_args.args[0].decode())
        assert "error" in reply
        assert "Internal server error" in reply["error"]
        assert "traceback" not in reply

    # Verify expose_internal_errors=True opt-in returns raw message and traceback
    config_opt_in = ServiceConfig(
        name="failing_opt_in", max_rpc_concurrency=3, expose_internal_errors=True
    )
    svc_opt_in = FailingService(config_opt_in)
    svc_opt_in._discover_handlers()
    svc_opt_in.container.lifecycle._running = True
    msg_opt_in = _rpc_msg("failing_opt_in.rpc.fail_work")
    await svc_opt_in.container._on_rpc_request(msg_opt_in)
    if svc_opt_in.container._active_tasks:
        await asyncio.gather(*list(svc_opt_in.container._active_tasks))
    reply_opt_in = json.loads(msg_opt_in.respond.call_args.args[0].decode())
    assert "Intentional handler failure" in reply_opt_in["error"]
    assert "traceback" in reply_opt_in


@pytest.mark.asyncio
async def test_semaphore_never_leaked_on_client_disconnect_or_respond_failure():
    """Verify that if msg.respond fails (e.g. client disconnected), semaphore permits are not leaked."""

    class EchoService(CliffracerService):
        @rpc
        async def echo(self, text: str) -> str:
            await asyncio.sleep(0.01)
            return text

    config = ServiceConfig(name="echo_svc", max_rpc_concurrency=3)
    svc = EchoService(config)
    svc._discover_handlers()
    svc.container.lifecycle._running = True

    sem = svc.container._get_rpc_semaphore()
    assert sem._value == 3

    # Messages where respond() raises an exception (broken pipe / connection closed)
    msgs = [
        _rpc_msg(
            "echo_svc.rpc.echo",
            data={"text": f"hello-{i}"},
            respond_side_effect=RuntimeError("Client closed connection"),
        )
        for i in range(15)
    ]
    tasks = [asyncio.create_task(svc.container._on_rpc_request(m)) for m in msgs]
    await asyncio.gather(*tasks)
    if getattr(svc.container, "_active_tasks", None):
        await asyncio.gather(*list(svc.container._active_tasks))
    await asyncio.sleep(0)

    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks), return_exceptions=True)

    # All permits returned despite respond() throwing
    assert sem._value == 3


@pytest.mark.asyncio
async def test_semaphore_never_leaked_on_task_cancellation():
    """Verify that externally cancelled RPC tasks release their semaphore permits."""

    class SlowService(CliffracerService):
        @rpc
        async def slow(self) -> str:
            await asyncio.sleep(10.0)
            return "never"

    config = ServiceConfig(name="slow_svc", max_rpc_concurrency=3)
    svc = SlowService(config)
    svc._discover_handlers()
    svc.container.lifecycle._running = True

    sem = svc.container._get_rpc_semaphore()
    assert sem._value == 3

    msgs = [_rpc_msg("slow_svc.rpc.slow") for _ in range(6)]
    tasks = [asyncio.create_task(svc.container._on_rpc_request(m)) for m in msgs]
    # Allow permits to be acquired
    await asyncio.sleep(0.05)

    # 3 tasks should be active in container
    active = list(svc.container._active_tasks)
    assert len(active) == 3
    # Cancel all active tasks
    for t in active:
        t.cancel()

    # Wait for container tasks to complete cancellation
    await asyncio.gather(*active, return_exceptions=True)
    # Remaining queued tasks can now proceed and be cancelled or finish
    await asyncio.sleep(0.05)
    remaining = list(svc.container._active_tasks)
    for t in remaining:
        t.cancel()
    if remaining:
        await asyncio.gather(*remaining, return_exceptions=True)

    await asyncio.gather(*tasks, return_exceptions=True)

    # Semaphore permits must be fully restored to 3
    assert sem._value == 3


@pytest.mark.asyncio
async def test_stress_mixed_failure_storm_preserves_semaphore_accounting():
    """Adversarial stress test: 80 concurrent requests under a semaphore bound of 5,
    combining success, handler errors, client disconnects, and external cancellations.
    """
    executed = 0

    class StormService(CliffracerService):
        @rpc
        async def action(self, op: str) -> str:
            nonlocal executed
            executed += 1
            if op == "fail":
                raise ValueError("Crash")
            if op == "hang":
                await asyncio.sleep(10.0)
                return "hung"
            await asyncio.sleep(0.01)
            return "ok"

    config = ServiceConfig(name="storm_svc", max_rpc_concurrency=5)
    svc = StormService(config)
    svc._discover_handlers()
    svc.container.lifecycle._running = True

    sem = svc.container._get_rpc_semaphore()
    assert sem._value == 5

    # 20 valid, 20 fail, 20 disconnect, 20 hang/cancel
    msgs = []
    for _ in range(20):
        msgs.append(_rpc_msg("storm_svc.rpc.action", data={"op": "valid"}))
    for _ in range(20):
        msgs.append(_rpc_msg("storm_svc.rpc.action", data={"op": "fail"}))
    for _ in range(20):
        msgs.append(
            _rpc_msg(
                "storm_svc.rpc.action",
                data={"op": "valid"},
                respond_side_effect=ConnectionResetError("Peer reset"),
            )
        )
    for _ in range(20):
        msgs.append(_rpc_msg("storm_svc.rpc.action", data={"op": "hang"}))

    tasks = [asyncio.create_task(svc.container._on_rpc_request(m)) for m in msgs]
    await asyncio.sleep(0)  # Let them all be created

    # Let the storm run briefly then cancel all active container tasks periodically
    for _ in range(5):
        await asyncio.sleep(0.03)
        for t in list(svc.container._active_tasks):
            if not t.done():
                t.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks), return_exceptions=True)

    # After the entire storm, the semaphore MUST have exactly 5 permits
    assert sem._value == 5

    # Now verify the service can cleanly process 5 new requests
    clean_msgs = [_rpc_msg("storm_svc.rpc.action", data={"op": "valid"}) for _ in range(5)]
    clean_tasks = [asyncio.create_task(svc.container._on_rpc_request(m)) for m in clean_msgs]
    await asyncio.gather(*clean_tasks)
    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert sem._value == 5
    for m in clean_msgs:
        reply = json.loads(m.respond.call_args.args[0].decode())
        assert reply["success"] is True
        assert reply["result"] == "ok"


@pytest.mark.asyncio
async def test_shutdown_timeout_hung_handler_cancels_within_deadline():
    """Simulate a hung RPC handler and confirm shutdown cancels it within shutdown_timeout."""
    was_cancelled = False

    class HungService(CliffracerService):
        @rpc
        async def hang(self) -> str:
            nonlocal was_cancelled
            try:
                await asyncio.Event().wait()  # Hangs indefinitely
                return "never"
            except asyncio.CancelledError:
                was_cancelled = True
                raise

    config = ServiceConfig(name="hung_svc", shutdown_timeout=0.25)
    svc = HungService(config)
    svc._discover_handlers()
    svc.container.lifecycle._running = True

    msg = _rpc_msg("hung_svc.rpc.hang")
    await svc.container._on_rpc_request(msg)
    assert len(svc.container._active_tasks) == 1

    start_time = time.time()
    await svc.stop()
    elapsed = time.time() - start_time

    assert 0.20 <= elapsed < 0.8, f"Shutdown took {elapsed}s, expected ~0.25s"
    assert was_cancelled is True
    assert len(svc.container._active_tasks) == 0
    assert svc._stopped is True


@pytest.mark.asyncio
async def test_shutdown_multiple_hung_handlers_all_cancelled():
    """Multiple concurrent hung tasks must all be cancelled cleanly on shutdown timeout."""
    cancelled_count = 0

    class MultiHungService(CliffracerService):
        @rpc
        async def hang(self) -> str:
            nonlocal cancelled_count
            try:
                await asyncio.sleep(100.0)
                return "never"
            except asyncio.CancelledError:
                cancelled_count += 1
                raise

    config = ServiceConfig(name="multi_hung_svc", shutdown_timeout=0.25, max_rpc_concurrency=10)
    svc = MultiHungService(config)
    svc._discover_handlers()
    svc.container.lifecycle._running = True

    msgs = [_rpc_msg("multi_hung_svc.rpc.hang") for _ in range(5)]
    tasks = [asyncio.create_task(svc.container._on_rpc_request(m)) for m in msgs]
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)

    assert len(svc.container._active_tasks) == 5

    start_time = time.time()
    await svc.stop()
    elapsed = time.time() - start_time

    assert 0.20 <= elapsed < 0.8
    assert cancelled_count == 5
    assert len(svc.container._active_tasks) == 0


# ============================================================================
# Section 2: Overlapping Listeners
# ============================================================================


class _MockEventMsg:
    def __init__(self, subject: str, data: dict):
        self.subject = subject
        self.data = json.dumps(data).encode()
        self.headers = None


@pytest.mark.asyncio
async def test_complex_multi_level_overlapping_patterns_exact_dispatch():
    """Test complex multi-level overlapping patterns:
    - `*.*`
    - `orders.*`
    - `orders.created`
    - `orders.cancelled`
    - `*.created`
    - `orders.>`

    Verify each handler executes exactly once per published matching event.
    """
    counts = {
        "all_wildcard": 0,
        "orders_wildcard": 0,
        "orders_created": 0,
        "orders_cancelled": 0,
        "any_created": 0,
        "orders_gt": 0,
    }

    class MultiOverlappingService(CliffracerService):
        @listener("*.*", fanout=True)
        async def on_all_wildcard(self, id: str = ""):
            counts["all_wildcard"] += 1

        @listener("orders.*", fanout=True)
        async def on_orders_wildcard(self, id: str = ""):
            counts["orders_wildcard"] += 1

        @listener("orders.created", fanout=True)
        async def on_orders_created(self, id: str = ""):
            counts["orders_created"] += 1

        @listener("orders.cancelled", fanout=True)
        async def on_orders_cancelled(self, id: str = ""):
            counts["orders_cancelled"] += 1

        @listener("*.created", fanout=True)
        async def on_any_created(self, id: str = ""):
            counts["any_created"] += 1

        @listener("orders.>", fanout=True)
        async def on_orders_gt(self, id: str = ""):
            counts["orders_gt"] += 1

    svc = MultiOverlappingService(ServiceConfig(name="multi_overlapping_svc"))
    svc._discover_handlers()

    # Generate the callbacks bound to each pattern (exactly as _setup_subscriptions does)
    cb_all = svc.container._make_event_callback("*.*")
    cb_orders = svc.container._make_event_callback("orders.*")
    cb_created = svc.container._make_event_callback("orders.created")
    cb_cancelled = svc.container._make_event_callback("orders.cancelled")
    cb_any_created = svc.container._make_event_callback("*.created")
    cb_orders_gt = svc.container._make_event_callback("orders.>")

    callbacks_map = {
        "*.*": cb_all,
        "orders.*": cb_orders,
        "orders.created": cb_created,
        "orders.cancelled": cb_cancelled,
        "*.created": cb_any_created,
        "orders.>": cb_orders_gt,
    }

    from cliffracer.core.subjects import subject_matches

    async def simulate_nats_publish(subject: str, payload: dict):
        """Simulate NATS broker delivering message to all registered subscription callbacks that match subject."""
        msg = _MockEventMsg(subject, payload)
        for pattern, cb in callbacks_map.items():
            if subject_matches(pattern, subject):
                await cb(msg)
        if getattr(svc.container, "_active_tasks", None):
            await asyncio.gather(*list(svc.container._active_tasks))

    # Case 1: Publish 'orders.created'
    # Matches: *.*, orders.*, orders.created, *.created, orders.> (5 matches, NOT orders.cancelled)
    await simulate_nats_publish("orders.created", {"id": "1"})
    assert counts["all_wildcard"] == 1
    assert counts["orders_wildcard"] == 1
    assert counts["orders_created"] == 1
    assert counts["orders_cancelled"] == 0
    assert counts["any_created"] == 1
    assert counts["orders_gt"] == 1

    # Case 2: Publish 'orders.cancelled'
    # Matches: *.*, orders.*, orders.cancelled, orders.> (4 matches)
    await simulate_nats_publish("orders.cancelled", {"id": "2"})
    assert counts["all_wildcard"] == 2
    assert counts["orders_wildcard"] == 2
    assert counts["orders_created"] == 1
    assert counts["orders_cancelled"] == 1
    assert counts["any_created"] == 1
    assert counts["orders_gt"] == 2

    # Case 3: Publish 'users.created'
    # Matches: *.*, *.created (2 matches)
    await simulate_nats_publish("users.created", {"id": "3"})
    assert counts["all_wildcard"] == 3
    assert counts["orders_wildcard"] == 2
    assert counts["orders_created"] == 1
    assert counts["orders_cancelled"] == 1
    assert counts["any_created"] == 2
    assert counts["orders_gt"] == 2

    # Case 4: Publish 'orders.us.priority.express' (4 tokens)
    # Matches: orders.> only (1 match)
    await simulate_nats_publish("orders.us.priority.express", {"id": "4"})
    assert counts["all_wildcard"] == 3
    assert counts["orders_wildcard"] == 2
    assert counts["orders_created"] == 1
    assert counts["orders_cancelled"] == 1
    assert counts["any_created"] == 2
    assert counts["orders_gt"] == 3


@pytest.mark.asyncio
async def test_overlapping_listeners_burst_stress():
    """Burst stress test: Send 100 rapid events to overlapping listeners."""
    created_count = 0
    wildcard_count = 0

    class FastService(CliffracerService):
        @listener("events.items.*", fanout=True)
        async def on_any(self, i: int = 0):
            nonlocal wildcard_count
            wildcard_count += 1

        @listener("events.items.created", fanout=True)
        async def on_created(self, i: int = 0):
            nonlocal created_count
            created_count += 1

    svc = FastService(ServiceConfig(name="fast_svc"))
    svc._discover_handlers()

    cb_any = svc.container._make_event_callback("events.items.*")
    cb_created = svc.container._make_event_callback("events.items.created")

    # Send 50 'events.items.created' and 50 'events.items.updated'
    for i in range(50):
        m_created = _MockEventMsg("events.items.created", {"i": i})
        await cb_any(m_created)
        await cb_created(m_created)

        m_updated = _MockEventMsg("events.items.updated", {"i": i})
        await cb_any(m_updated)
        # NATS would NOT deliver m_updated to cb_created

    if getattr(svc.container, "_active_tasks", None):
        await asyncio.gather(*list(svc.container._active_tasks))

    assert created_count == 50
    assert wildcard_count == 100


# ============================================================================
# Section 3: Malformed JSON on Durable Listeners
# ============================================================================


class TargetItem(BaseModel):
    id: str
    count: int


def _js_msg(subject: str, data: bytes, num_delivered: int = 1):
    msg = AsyncMock()
    msg.subject = subject
    msg.data = data
    msg.headers = None
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    return msg


@pytest.mark.asyncio
async def test_adversarial_payloads_on_durable_validated_listener():
    """Adversarial payloads tested against a durable @validated_listener:
    1. Completely invalid bytes (b"\\x80\\xff")
    2. Empty payload (b"")
    3. Truncated malformed JSON (b'{"key": ')
    4. Whitespace only (b"   ")
    5. Wrong data type: JSON array (b"[1, 2, 3]")
    6. Wrong data type: JSON integer (b"12345")
    7. Wrong data type: JSON string (b'"just a string"')
    8. Wrong field data type (b'{"id": "abc", "count": "not_an_int"}')
    9. Missing required field (b'{"count": 5}')

    Verify that EVERY ONE is routed to DLQ and terminated from JetStream without hanging or acknowledging.
    """
    published_dlq = []

    class SecureService(CliffracerService):
        @validated_listener("events.items", TargetItem, durable="secure-processor")
        async def on_item(self, message: TargetItem):
            pass

    config = ServiceConfig(
        name="secure_svc",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="ITEMS", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )
    svc = SecureService(config)
    svc.nc = AsyncMock()
    svc.js = AsyncMock()
    svc._discover_handlers()

    async def mock_publish_event(subject, **kwargs):
        published_dlq.append((subject, kwargs))

    svc.publish_event = mock_publish_event
    svc.container._publish_dlq = mock_publish_event

    adversarial_payloads = [
        ("invalid_bytes", b"\x80\xff"),
        ("empty_payload", b""),
        ("truncated_json", b'{"key": '),
        ("whitespace_payload", b"   "),
        ("json_array", b"[1, 2, 3]"),
        ("json_integer", b"12345"),
        ("json_string", b'"just a string"'),
        ("wrong_field_type", b'{"id": "abc", "count": "not_an_int"}'),
        ("missing_field", b'{"count": 5}'),
    ]

    for label, payload in adversarial_payloads:
        published_dlq.clear()
        msg = _js_msg("events.items", payload)

        await svc.container._handle_jetstream_event(msg, pattern="events.items")

        # 1. Message MUST be terminated exactly once
        assert msg.term.await_count == 1, f"Failed for {label}: msg.term() was not called"
        # 2. Message MUST NOT be acked
        assert msg.ack.await_count == 0, f"Failed for {label}: msg.ack() was called"
        # 3. Message MUST NOT be naked
        assert msg.nak.await_count == 0, f"Failed for {label}: msg.nak() was called"
        # 4. Message MUST be published to DLQ
        assert len(published_dlq) == 1, f"Failed for {label}: DLQ event not published"
        dlq_subject, kwargs = published_dlq[0]
        assert dlq_subject == "dlq.secure_svc"
        assert kwargs["service"] == "secure_svc"


@pytest.mark.asyncio
async def test_adversarial_decode_payloads_on_durable_unvalidated_listener():
    """Adversarial decode payloads tested against a durable unvalidated @listener:
    1. Completely invalid bytes (b"\\x80\\xff")
    2. Malformed truncated JSON (b'{"key": ')
    3. Whitespace only (b"   ")

    Verify they are routed to DLQ and terminated from JetStream without hanging or acknowledging.
    """
    published_dlq = []

    class RawListenerService(CliffracerService):
        @listener("events.raw", durable="raw-processor")
        async def on_raw(self, item: str = ""):
            pass

    config = ServiceConfig(
        name="raw_svc",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="RAW", subjects=["events.*"]),
            StreamSpec(name="DLQ", subjects=["dlq.*"]),
        ],
    )
    svc = RawListenerService(config)
    svc.nc = AsyncMock()
    svc.js = AsyncMock()
    svc._discover_handlers()

    async def mock_publish_event(subject, **kwargs):
        published_dlq.append((subject, kwargs))

    svc.publish_event = mock_publish_event
    svc.container._publish_dlq = mock_publish_event

    bad_payloads = [
        ("invalid_bytes", b"\x80\xff"),
        ("truncated_json", b'{"key": '),
        ("whitespace_payload", b"   "),
    ]

    for label, payload in bad_payloads:
        published_dlq.clear()
        msg = _js_msg("events.raw", payload)

        await svc.container._handle_jetstream_event(msg, pattern="events.raw")

        assert msg.term.await_count == 1, f"Failed for {label}: msg.term() was not called"
        assert msg.ack.await_count == 0, f"Failed for {label}: msg.ack() was called"
        assert msg.nak.await_count == 0, f"Failed for {label}: msg.nak() was called"
        assert len(published_dlq) == 1, f"Failed for {label}: DLQ event not published"
        assert published_dlq[0][0] == "dlq.raw_svc"
        assert "Decode error" in published_dlq[0][1]["error"]
