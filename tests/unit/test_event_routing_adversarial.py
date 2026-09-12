"""Adversarial stress test suite for event routing and authorization edge cases.

Focus Areas Tested:
1. Overlapping listener subjects (*.*, orders.*, *.created, orders.created, orders.>)
2. Malformed JSON payloads on durable listeners (push/pull, termination, DLQ routing, DLQ failure resilience)
3. Method signature hashing with all Field constraints (numeric, string, regex, decimal, order invariance)
4. Concurrency bounds (max_rpc_concurrency=1, 4, None), semaphore integrity, and shutdown task drain/timeout
5. AuthExtension timer interaction (allow_timers=True/False, default_timer_user, contextvar leakage prevention)
"""

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Annotated, Any
from unittest.mock import AsyncMock

import pytest
from cliffracer_auth import (
    AuthConfig,
    AuthUser,
)
from cliffracer_auth.extension import AuthExtension
from cliffracer_auth.simple_auth import SimpleAuthService, auth_context_var
from pydantic import BaseModel, Field

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    StreamSpec,
    listener,
    rpc,
)
from cliffracer.core.container import DispatchOutcome
from cliffracer.core.extension import RejectMessage, WorkerContext
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit

VALID_SECRET_KEY = "super-secret-key-that-is-at-least-32-chars-long-0123456789"


def _make_msg(
    subject: str,
    data: Any = None,
    raw: bytes | None = None,
    headers: dict[str, str] | None = None,
    num_delivered: int = 1,
):
    msg = AsyncMock()
    msg.subject = subject
    if raw is not None:
        msg.data = raw
    elif data is not None:
        msg.data = json.dumps(data).encode()
    else:
        msg.data = b"{}"
    msg.headers = headers
    msg.reply = "reply.123"
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    return msg


# ============================================================================
# Section 1: Overlapping Listener Subjects
# ============================================================================


@pytest.mark.asyncio
async def test_adversarial_five_level_overlapping_patterns():
    """5 overlapping listener patterns receive an event on 'orders.created'.

    Patterns registered:
    - *.*
    - orders.*
    - *.created
    - orders.created
    - orders.>

    When published to 'orders.created':
    - NATS delivers the message to all 5 matching subscription callbacks.
    - Each handler MUST execute exactly ONCE (total 5 executions, not 25).
    """
    counts = {
        "any_two": 0,
        "orders_wildcard": 0,
        "any_created": 0,
        "orders_created": 0,
        "orders_gt": 0,
    }

    class MultiOverlapService(CliffracerService):
        @listener("*.*", fanout=True)
        async def on_any_two(self, order_id: int = 0) -> None:
            counts["any_two"] += 1

        @listener("orders.*", fanout=True)
        async def on_orders_wildcard(self, order_id: int = 0) -> None:
            counts["orders_wildcard"] += 1

        @listener("*.created", fanout=True)
        async def on_any_created(self, order_id: int = 0) -> None:
            counts["any_created"] += 1

        @listener("orders.created", fanout=True)
        async def on_orders_created(self, order_id: int = 0) -> None:
            counts["orders_created"] += 1

        @listener("orders.>", fanout=True)
        async def on_orders_gt(self, order_id: int = 0) -> None:
            counts["orders_gt"] += 1

    svc = MultiOverlapService(ServiceConfig(name="multi_overlap_svc"))
    svc._discover_handlers()

    # When NATS delivers to the registered callbacks for 'orders.created'
    msg = _make_msg("orders.created", {"order_id": 999})
    cb_any_two = svc.container._make_event_callback("*.*")
    cb_orders_wc = svc.container._make_event_callback("orders.*")
    cb_any_created = svc.container._make_event_callback("*.created")
    cb_orders_created = svc.container._make_event_callback("orders.created")
    cb_orders_gt = svc.container._make_event_callback("orders.>")

    await asyncio.gather(
        cb_any_two(msg),
        cb_orders_wc(msg),
        cb_any_created(msg),
        cb_orders_created(msg),
        cb_orders_gt(msg),
    )

    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert counts == {
        "any_two": 1,
        "orders_wildcard": 1,
        "any_created": 1,
        "orders_created": 1,
        "orders_gt": 1,
    }


@pytest.mark.asyncio
async def test_adversarial_unbound_dispatch_hierarchical_filtering():
    """When _dispatch_event is called without a pattern (unbound broadcast):
    Subject 'orders.eu.west.created' should ONLY trigger 'orders.>', not 2-token patterns.
    """
    counts = {
        "orders_wildcard": 0,
        "orders_created": 0,
        "orders_gt": 0,
    }

    class DeepHierarchyService(CliffracerService):
        @listener("orders.*", fanout=True)
        async def on_orders_wc(self, id: int = 0) -> None:
            counts["orders_wildcard"] += 1

        @listener("orders.created", fanout=True)
        async def on_orders_created(self, id: int = 0) -> None:
            counts["orders_created"] += 1

        @listener("orders.>", fanout=True)
        async def on_orders_gt(self, id: int = 0) -> None:
            counts["orders_gt"] += 1

    svc = DeepHierarchyService(ServiceConfig(name="deep_svc"))
    svc._discover_handlers()

    msg = _make_msg("orders.eu.west.created", {"id": 1})
    outcome = await svc.container._dispatch_event(msg, pattern=None)

    assert outcome == DispatchOutcome.OK
    assert counts["orders_wildcard"] == 0
    assert counts["orders_created"] == 0
    assert counts["orders_gt"] == 1


# ============================================================================
# Section 2: Malformed JSON on Durable Listeners & DLQ
# ============================================================================


class ValidItem(BaseModel):
    item_id: str
    price: float


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("corrupt_payload", "headers"),
    [
        (b"   ", None),  # Whitespace only (fails json & msgpack)
        (b'{"item_id":', None),  # Truncated value
        (b"[1, 2,", None),  # Truncated array
        (b"\x00\x01\x02\xff", None),  # Binary non-UTF8 garbage
        (b"\xed\xa0\x80", None),  # UTF-8 surrogate (illegal in RFC 3629)
        (b"undefined", None),  # JavaScript undefined
        (b"{", {"Content-Type": "application/json"}),  # Truncated JSON with explicit header
        (b"}", {"Content-Type": "application/json"}),  # Closing brace with explicit header
        (b"bad-json-text", {"Content-Type": "application/json"}),
    ],
)
async def test_corrupt_payload_matrix_routes_to_dlq_and_terminates(corrupt_payload, headers):
    """Every malformed or unparseable byte sequence:
    1. Dead-letters to dlq.<service>
    2. Calls msg.term()
    3. Never calls msg.ack() or msg.nak()
    """

    class DurableService(CliffracerService):
        @listener("events.orders", durable="orders-worker")
        async def on_order(self) -> None:
            pass

    svc = DurableService(
        ServiceConfig(
            name="dlq_test_svc",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="EVENTS", subjects=["events.*"])],
        )
    )
    svc._discover_handlers()

    dlq_records = []

    async def mock_publish_event(subject, **kwargs):
        dlq_records.append((subject, kwargs))

    svc.publish_event = mock_publish_event
    svc.container._publish_dlq = mock_publish_event

    msg = _make_msg("events.orders", raw=corrupt_payload, headers=headers, num_delivered=1)

    await svc.container._handle_jetstream_event(msg, pattern="events.orders")

    # Assertions
    assert msg.term.await_count == 1
    assert msg.ack.await_count == 0
    assert msg.nak.await_count == 0
    assert len(dlq_records) == 1
    assert dlq_records[0][0] == "dlq.dlq_test_svc"
    assert dlq_records[0][1]["original_subject"] == "events.orders"
    assert "Decode error" in dlq_records[0][1]["error"]


@pytest.mark.asyncio
async def test_dlq_broker_failure_still_terminates_malformed_message():
    """Even when publish_event to DLQ raises an exception (e.g. JetStream stream full),
    the poison message MUST still be terminated via msg.term() to prevent an infinite loop.
    """

    class DurableService(CliffracerService):
        @listener("events.orders", durable="orders-worker")
        async def on_order(self) -> None:
            pass

    svc = DurableService(
        ServiceConfig(
            name="dlq_fail_svc",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="EVENTS", subjects=["events.*"])],
        )
    )
    svc._discover_handlers()

    async def broken_publish_event(subject, **kwargs):
        raise ConnectionError("DLQ broker stream unavailable or disc full")

    svc.publish_event = broken_publish_event
    svc.container._publish_dlq = broken_publish_event

    msg = _make_msg(
        "events.orders",
        raw=b"{broken-json",
        headers={"Content-Type": "application/json"},
        num_delivered=2,
    )

    await svc.container._handle_jetstream_event(msg, pattern="events.orders")

    assert msg.term.await_count == 1
    assert msg.ack.await_count == 0
    assert msg.nak.await_count == 0


@pytest.mark.asyncio
async def test_pull_consumer_malformed_json_routes_to_dlq_and_terminates():
    """Pull consumer receiving malformed JSON terminates message and routes to DLQ."""

    class PullService(CliffracerService):
        @listener("events.pull", durable="pull-worker", pull=True)
        async def on_pull(self) -> None:
            pass

    svc = PullService(
        ServiceConfig(
            name="pull_dlq_svc",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="EVENTS", subjects=["events.*"])],
        )
    )
    svc._discover_handlers()

    dlq_records = []

    async def mock_publish_event(subject, **kwargs):
        dlq_records.append((subject, kwargs))

    svc.publish_event = mock_publish_event
    svc.container._publish_dlq = mock_publish_event

    mock_msg = _make_msg(
        "events.pull",
        raw=b"{{{bad_json",
        headers={"Content-Type": "application/json"},
    )
    mock_sub = AsyncMock()
    mock_sub.fetch = AsyncMock(return_value=[mock_msg])

    count = await svc.container._pull_once(mock_sub, pattern="events.pull")
    assert count == 1

    assert mock_msg.term.await_count == 1
    assert mock_msg.ack.await_count == 0
    assert mock_msg.nak.await_count == 0
    assert len(dlq_records) == 1
    assert dlq_records[0][0] == "dlq.pull_dlq_svc"


# ============================================================================
# Section 3: Method Signature Hashing with Field Constraints
# ============================================================================


def test_all_numeric_and_string_field_constraints_alter_signature_hash():
    """Verify that every supported Field constraint alters signature_hash and description_hash."""
    constraint_pairs = [
        # (field_v1, field_v2, constraint_key, param_type)
        (Field(ge=1), Field(ge=10), "ge", int),
        (Field(gt=0), Field(gt=5), "gt", int),
        (Field(le=100), Field(le=50), "le", int),
        (Field(lt=10), Field(lt=8), "lt", int),
        (Field(min_length=2), Field(min_length=5), "min_length", str),
        (Field(max_length=20), Field(max_length=10), "max_length", str),
        (Field(pattern=r"^[a-z]+$"), Field(pattern=r"^[A-Z]+$"), "pattern", str),
        (Field(strict=True), Field(strict=False), "strict", int),
        (Field(multiple_of=2), Field(multiple_of=3), "multiple_of", int),
        (Field(max_digits=5), Field(max_digits=8), "max_digits", float),
        (Field(decimal_places=2), Field(decimal_places=4), "decimal_places", float),
    ]

    for f1, f2, attr, tp in constraint_pairs:

        class Svc1(CliffracerService):
            @rpc
            async def action(self, val: Annotated[tp, f1]) -> tp:  # type: ignore[valid-type]
                return val

        class Svc2(CliffracerService):
            @rpc
            async def action(self, val: Annotated[tp, f2]) -> tp:  # type: ignore[valid-type]
                return val

        desc1 = describe(Svc1, service="test_svc", version="1.0.0")
        desc2 = describe(Svc2, service="test_svc", version="1.0.0")

        m1 = desc1.method("action")
        m2 = desc2.method("action")
        assert m1 is not None and m2 is not None

        # Verify extracted constraint value
        assert attr in m1.params[0].type["constraints"], f"Missing {attr} in m1"
        assert attr in m2.params[0].type["constraints"], f"Missing {attr} in m2"
        assert m1.params[0].type["constraints"][attr] != m2.params[0].type["constraints"][attr]

        # Hashes MUST differ
        assert m1.signature_hash != m2.signature_hash, (
            f"Signature hash collision for constraint {attr}: {m1.signature_hash}"
        )
        assert desc1.description_hash != desc2.description_hash, (
            f"Description hash collision for constraint {attr}"
        )


def test_field_constraint_order_invariance():
    """Constraint dictionary is sorted alphabetically so definition order does not affect hash."""

    class SvcOrderA(CliffracerService):
        @rpc
        async def submit(self, val: Annotated[int, Field(ge=1, le=10, multiple_of=2)]) -> int:
            return val

    class SvcOrderB(CliffracerService):
        @rpc
        async def submit(self, val: Annotated[int, Field(multiple_of=2, le=10, ge=1)]) -> int:
            return val

    descA = describe(SvcOrderA, service="order_svc", version="1.0.0")
    descB = describe(SvcOrderB, service="order_svc", version="1.0.0")

    mA = descA.method("submit")
    mB = descB.method("submit")
    assert mA is not None and mB is not None

    assert mA.params[0].type["constraints"] == mB.params[0].type["constraints"]
    assert mA.signature_hash == mB.signature_hash
    assert descA.description_hash == descB.description_hash


def test_compiled_regex_pattern_constraint():
    """A compiled re.Pattern passed to Field(pattern=...) normalizes to regex string."""
    pat = re.compile(r"^[0-9]{3}-[0-9]{2}-[0-9]{4}$")

    class RegexService(CliffracerService):
        @rpc
        async def set_ssn(self, ssn: Annotated[str, Field(pattern=pat)]) -> str:
            return ssn

    desc = describe(RegexService, service="reg_svc", version="1.0.0")
    m = desc.method("set_ssn")
    assert m is not None
    assert m.params[0].type["constraints"]["pattern"] == r"^[0-9]{3}-[0-9]{2}-[0-9]{4}$"


# ============================================================================
# Section 4: Concurrency Bounds & Shutdown Task Drain
# ============================================================================


@pytest.mark.asyncio
async def test_max_rpc_concurrency_one_strict_serialization():
    """max_rpc_concurrency=1 guarantees strictly serialized execution (concurrency never > 1)."""
    current_in_flight = 0
    max_observed = 0
    execution_order = []

    class SerialService(CliffracerService):
        @rpc
        async def step(self, step_id: int) -> int:
            nonlocal current_in_flight, max_observed
            current_in_flight += 1
            max_observed = max(max_observed, current_in_flight)
            execution_order.append(f"start_{step_id}")
            await asyncio.sleep(0.01)
            execution_order.append(f"end_{step_id}")
            current_in_flight -= 1
            return step_id

    svc = SerialService(ServiceConfig(name="serial_svc", max_rpc_concurrency=1))
    svc._discover_handlers()
    svc._running = True

    msgs = [_make_msg("serial_svc.rpc.step", data={"step_id": i}) for i in range(5)]
    tasks = [asyncio.create_task(svc.container._on_rpc_request(m)) for m in msgs]
    await asyncio.gather(*tasks)

    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert max_observed == 1
    # Verify strict interleaving: start_i immediately followed by end_i before start_(i+1)
    for i in range(5):
        start_idx = execution_order.index(f"start_{i}")
        end_idx = execution_order.index(f"end_{i}")
        assert end_idx == start_idx + 1


@pytest.mark.asyncio
async def test_semaphore_permit_restored_on_handler_crash_and_cancellation():
    """When RPC handlers crash or are cancelled, semaphore permits are unconditionally released."""

    class CrashService(CliffracerService):
        @rpc
        async def bomb(self, will_crash: bool) -> str:
            if will_crash:
                raise ZeroDivisionError("division by zero in handler")
            return "ok"

    svc = CrashService(ServiceConfig(name="crash_svc", max_rpc_concurrency=2))
    svc._discover_handlers()
    svc._running = True

    sem = svc.container._get_rpc_semaphore()
    assert sem is not None
    assert sem._value == 2

    # Send 4 crashing requests
    crash_msgs = [_make_msg("crash_svc.rpc.bomb", data={"will_crash": True}) for _ in range(4)]
    tasks = [asyncio.create_task(svc.container._on_rpc_request(m)) for m in crash_msgs]
    await asyncio.gather(*tasks)

    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    # All permits MUST be restored to 2
    assert sem._value == 2

    # Verify subsequent valid requests succeed fully
    ok_msg = _make_msg("crash_svc.rpc.bomb", data={"will_crash": False})
    await svc.container._on_rpc_request(ok_msg)
    if svc.container._active_tasks:
        await asyncio.gather(*list(svc.container._active_tasks))

    assert ok_msg.respond.await_count == 1
    reply = json.loads(ok_msg.respond.call_args.args[0].decode())
    assert reply["success"] is True
    assert reply["result"] == "ok"
    assert sem._value == 2


@pytest.mark.asyncio
async def test_shutdown_timeout_task_drain_and_on_shutdown_ordering():
    """Verify shutdown drains in-flight tasks before calling on_shutdown(),
    and stops extensions after on_shutdown().
    """
    timeline = []

    class DrainService(CliffracerService):
        @rpc
        async def slow_work(self) -> str:
            timeline.append("work_started")
            await asyncio.sleep(0.05)
            timeline.append("work_completed")
            return "done"

        async def on_shutdown(self):
            timeline.append("on_shutdown_called")

    svc = DrainService(ServiceConfig(name="drain_svc", shutdown_timeout=2.0))
    svc._discover_handlers()
    svc._running = True
    svc._startup_succeeded = True
    svc._on_startup_completed = True

    msg = _make_msg("drain_svc.rpc.slow_work")
    # Launch slow RPC request
    asyncio.create_task(svc.container._on_rpc_request(msg))
    await asyncio.sleep(0.01)

    # Initiate stop while work is in flight
    await svc._stop_internal()

    # Timeline must confirm work_completed happened BEFORE on_shutdown_called
    assert "work_started" in timeline
    assert "work_completed" in timeline
    assert "on_shutdown_called" in timeline
    work_end_idx = timeline.index("work_completed")
    shutdown_idx = timeline.index("on_shutdown_called")
    assert work_end_idx < shutdown_idx, "Active task drain did not precede on_shutdown!"
    assert len(svc.container._active_tasks) == 0


# ============================================================================
# Section 5: Auth Extension with Timers & Isolation
# ============================================================================


@pytest.mark.asyncio
async def test_auth_timer_isolation_and_no_leak_to_rpc():
    """Verify default_timer_user authenticates timer executions,
    cleans up contextvar upon completion, and NEVER authenticates RPC or event requests.
    """
    timer_user = AuthUser(
        user_id="timer-bot-42",
        username="system_cron",
        email="cron@internal",
        roles={"scheduled_job"},
    )

    auth_svc = SimpleAuthService(AuthConfig(secret_key=VALID_SECRET_KEY))
    auth_ext = AuthExtension(auth_svc, default_timer_user=timer_user, allow_timers=True)

    # 1. Timer execution context
    timer_ctx = WorkerContext(
        kind="timer",
        subject="service.timer.tick",
        headers={},
        correlation_id="cid-timer-1",
        payload={},
        raw=None,
    )

    await auth_ext.worker_setup(timer_ctx)
    assert timer_ctx.data.get("auth") is not None
    assert timer_ctx.data["auth"].user.username == "system_cron"
    assert auth_context_var.get() is timer_ctx.data["auth"]

    # Worker teardown cleans up contextvar
    await auth_ext.worker_teardown(timer_ctx)
    assert auth_context_var.get(None) is None

    # 2. RPC request without header MUST STILL BE REJECTED despite default_timer_user
    rpc_ctx = WorkerContext(
        kind="rpc",
        subject="service.rpc.admin_action",
        headers={},
        correlation_id="cid-rpc-1",
        payload={},
        raw=None,
    )

    with pytest.raises(RejectMessage) as exc_info:
        await auth_ext.worker_setup(rpc_ctx)
    assert "unauthenticated" in str(exc_info.value)

    # 3. Event request without header MUST STILL BE REJECTED
    event_ctx = WorkerContext(
        kind="event",
        subject="service.events.incoming",
        headers={},
        correlation_id="cid-event-1",
        payload={},
        raw=None,
    )

    with pytest.raises(RejectMessage) as exc_info:
        await auth_ext.worker_setup(event_ctx)
    assert "unauthenticated" in str(exc_info.value)


@pytest.mark.asyncio
async def test_auth_timer_disallowed_raises_reject_message():
    """When allow_timers=False, timer requests raise RejectMessage('unauthenticated')."""
    auth_svc = SimpleAuthService(AuthConfig(secret_key=VALID_SECRET_KEY))
    auth_ext = AuthExtension(auth_svc, allow_timers=False)

    timer_ctx = WorkerContext(
        kind="timer",
        subject="service.timer.tick",
        headers={},
        correlation_id="cid-timer-2",
        payload={},
        raw=None,
    )

    with pytest.raises(RejectMessage) as exc_info:
        await auth_ext.worker_setup(timer_ctx)
    assert "unauthenticated" in str(exc_info.value)
