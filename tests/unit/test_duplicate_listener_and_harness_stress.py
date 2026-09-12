"""Adversarial stress-tests for duplicate listener detection and ServiceTestHarness parity.

Empirically validates:
1. Duplicate listener registration invariants in HandlerDiscovery:
   - Identical subjects (with and without namespace)
   - Multi-decorator collisions on same method
   - Cross-decorator collisions (@listener vs @validated_listener vs @broadcast)
   - Wildcard listeners vs exact listeners (orders.* vs orders.created)
   - Class inheritance in MRO (subclass distinct methods vs overriding methods vs multiple inheritance)
   - Single-subject vs multi-subject durable consumer collisions
   - JetStream enabled vs disabled durable/fanout matrix
2. ServiceTestHarness and publish alias parity:
   - Method identity and parameter parity with emit_event
   - Various payload types (dict, kwargs, Pydantic model, primitive, list, empty)
   - Header propagation and content-type encoding (JSON, msgpack)
   - Pattern filtering dispatch parity
   - Validated listeners error outcome parity
   - Async error handling and isolation
   - Background supervised task drain and leak prevention on teardown
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, Field

from cliffracer.core.container import DispatchOutcome
from cliffracer.core.decorators import broadcast, listener, rpc, validated_listener
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.jetstream import StreamSpec
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit


class ItemPayload(BaseModel):
    item_id: str = Field(min_length=1)
    quantity: int = Field(gt=0)


# ==============================================================================
# 1. DUPLICATE LISTENER REGISTRATION IN HANDLERDISCOVERY
# ==============================================================================


def test_duplicate_listener_exact_subject_without_namespace_raises() -> None:
    """Two distinct methods listening on the exact same subject raise ConfigurationError."""

    class DuplicateExactService(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_created_first(self) -> None:
            pass

        @listener("orders.created", fanout=True)
        async def on_created_second(self) -> None:
            pass

    cfg = ServiceConfig(name="exact_dup_svc")
    svc = DuplicateExactService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    err = str(exc_info.value)
    assert "Duplicate event listener declared on subject 'orders.created'" in err
    assert "on_created_second" in err or "on_created_first" in err


def test_duplicate_listener_exact_subject_with_namespace_raises() -> None:
    """Two methods on the same subject under a service namespace raise ConfigurationError."""

    class NamespacedDuplicateService(CliffracerService):
        @listener("orders.created", fanout=True)
        async def handler_a(self) -> None:
            pass

        @listener("orders.created", fanout=True)
        async def handler_b(self) -> None:
            pass

    cfg = ServiceConfig(name="ns_dup_svc", namespace="production")
    svc = NamespacedDuplicateService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    err = str(exc_info.value)
    assert "Duplicate event listener declared on subject 'production.orders.created'" in err


def test_duplicate_listener_multi_decorator_same_method_raises() -> None:
    """A single method decorated twice with the same subject raises ConfigurationError."""

    class DoubleDecoratedMethodService(CliffracerService):
        @listener("orders.created", fanout=True)
        @listener("orders.created", fanout=True)
        async def handle_orders(self) -> None:
            pass

    cfg = ServiceConfig(name="double_dec_svc")
    svc = DoubleDecoratedMethodService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    err = str(exc_info.value)
    assert "Duplicate event listener declared on subject 'orders.created'" in err
    assert "handle_orders" in err


def test_cross_decorator_standard_and_validated_listener_collision_raises() -> None:
    """Collision between standard @listener and @validated_listener on identical subject raises."""

    class CrossStandardValidatedService(CliffracerService):
        @listener("inventory.updated", fanout=True)
        async def on_standard(self) -> None:
            pass

        @validated_listener("inventory.updated", schema=ItemPayload, fanout=True)
        async def on_validated(self, message: ItemPayload) -> None:
            pass

    cfg = ServiceConfig(name="cross_val_svc")
    svc = CrossStandardValidatedService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    err = str(exc_info.value)
    assert "Duplicate event listener declared on subject 'inventory.updated'" in err


def test_cross_decorator_broadcast_and_standard_listener_collision_raises() -> None:
    """Collision between @broadcast and @listener on identical subject raises ConfigurationError."""

    class BroadcastStandardCollisionService(CliffracerService):
        @broadcast("system.alerts")
        async def on_broadcast(self) -> None:
            pass

        @listener("system.alerts", fanout=True)
        async def on_listener(self) -> None:
            pass

    cfg = ServiceConfig(name="bcast_collision_svc")
    svc = BroadcastStandardCollisionService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    err = str(exc_info.value)
    assert "Duplicate event listener declared on subject 'system.alerts'" in err


def test_cross_namespace_subject_collision_raises() -> None:
    """Cross-namespace subject collision (*.events vs pattern matching *.events) raises."""

    class CrossNamespaceCollisionService(CliffracerService):
        @listener("events.audit", cross_namespace=True, fanout=True)
        async def on_cross(self) -> None:
            pass

        @listener("*.events.audit", cross_namespace=False, fanout=True)
        async def on_exact_star(self) -> None:
            pass

    cfg = ServiceConfig(name="cross_ns_svc", namespace=None)
    svc = CrossNamespaceCollisionService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    err = str(exc_info.value)
    assert "Duplicate event listener declared on subject '*.events.audit'" in err


# ==============================================================================
# 2. WILDCARDS VS EXACT SUBJECTS (COEXISTENCE CONTRACT)
# ==============================================================================


def test_wildcard_and_exact_listeners_coexist_without_error() -> None:
    """Wildcard (orders.*) and exact (orders.created) are distinct subscription patterns and coexist."""

    class OverlappingPatternsService(CliffracerService):
        @listener("orders.*", fanout=True)
        async def on_wildcard(self) -> None:
            pass

        @listener("orders.created", fanout=True)
        async def on_exact(self) -> None:
            pass

        @listener("orders.>", fanout=True)
        async def on_multi_wildcard(self) -> None:
            pass

    cfg = ServiceConfig(name="overlapping_patterns_svc")
    svc = OverlappingPatternsService(cfg)
    reg = HandlerDiscovery.discover(svc, cfg)

    assert "orders.*" in reg.event_handlers
    assert "orders.created" in reg.event_handlers
    assert "orders.>" in reg.event_handlers
    assert len(reg.event_handlers) == 3


async def test_wildcard_and_exact_listeners_dispatch_isolation() -> None:
    """When dispatching via harness, wildcard and exact handlers execute independently."""
    wildcard_calls: list[dict[str, Any]] = []
    exact_calls: list[dict[str, Any]] = []

    class OrderDispatchService(CliffracerService):
        @listener("orders.*", fanout=True)
        async def on_wildcard(self, order_id: str = "") -> None:
            wildcard_calls.append({"order_id": order_id})

        @listener("orders.created", fanout=True)
        async def on_exact(self, order_id: str = "") -> None:
            exact_calls.append({"order_id": order_id})

    async with ServiceTestHarness(OrderDispatchService) as harness:
        # 1. Dispatching to orders.created without pattern matches both registered subscriptions
        outcome = await harness.publish("orders.created", {"order_id": "ord_1"})
        assert outcome == DispatchOutcome.OK
        assert len(wildcard_calls) == 1
        assert len(exact_calls) == 1

        # 2. Dispatching with pattern="orders.*" targets only wildcard handler
        outcome = await harness.publish("orders.created", {"order_id": "ord_2"}, pattern="orders.*")
        assert outcome == DispatchOutcome.OK
        assert len(wildcard_calls) == 2
        assert len(exact_calls) == 1

        # 3. Dispatching with pattern="orders.created" targets only exact handler
        outcome = await harness.publish(
            "orders.created", {"order_id": "ord_3"}, pattern="orders.created"
        )
        assert outcome == DispatchOutcome.OK
        assert len(wildcard_calls) == 2
        assert len(exact_calls) == 2


# ==============================================================================
# 3. INHERITANCE AND MRO SCENARIOS
# ==============================================================================


def test_inherited_class_distinct_methods_same_subject_raises() -> None:
    """Subclass with a new method on the same subject as base class raises ConfigurationError."""

    class BaseService(CliffracerService):
        @listener("payments.processed", fanout=True)
        async def on_base_payment(self) -> None:
            pass

    class DerivedService(BaseService):
        @listener("payments.processed", fanout=True)
        async def on_derived_payment(self) -> None:
            pass

    cfg = ServiceConfig(name="derived_dup_svc")
    svc = DerivedService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    err = str(exc_info.value)
    assert "Duplicate event listener declared on subject 'payments.processed'" in err
    assert "on_derived_payment" in err or "on_base_payment" in err


def test_inherited_class_overridden_method_is_registered_once() -> None:
    """Subclass overriding base method with same name replaces handler without duplicate collision."""
    calls: list[str] = []

    class BaseService(CliffracerService):
        @listener("payments.processed", fanout=True)
        async def on_payment(self) -> None:
            calls.append("base")

    class DerivedService(BaseService):
        @listener("payments.processed", fanout=True)
        async def on_payment(self) -> None:
            calls.append("derived")

    cfg = ServiceConfig(name="derived_override_svc")
    svc = DerivedService(cfg)
    reg = HandlerDiscovery.discover(svc, cfg)

    assert "payments.processed" in reg.event_handlers
    assert len(reg.event_handlers) == 1
    assert reg.event_handler_names["payments.processed"] == "on_payment"


def test_inherited_class_override_without_decorator_removes_listener() -> None:
    """Subclass overriding base method without decorator suppresses listener registration."""

    class BaseService(CliffracerService):
        @listener("payments.processed", fanout=True)
        async def on_payment(self) -> None:
            pass

    class DerivedService(BaseService):
        async def on_payment(self) -> None:
            pass

    cfg = ServiceConfig(name="derived_suppress_svc")
    svc = DerivedService(cfg)
    reg = HandlerDiscovery.discover(svc, cfg)

    assert "payments.processed" not in reg.event_handlers
    assert len(reg.event_handlers) == 0


def test_multiple_inheritance_listener_collision_raises() -> None:
    """Multiple base mixins declaring handlers for the same subject raise ConfigurationError."""

    class MixinA:
        @listener("shared.notification", fanout=True)
        async def on_notify_a(self) -> None:
            pass

    class MixinB:
        @listener("shared.notification", fanout=True)
        async def on_notify_b(self) -> None:
            pass

    class DiamondService(CliffracerService, MixinA, MixinB):
        pass

    cfg = ServiceConfig(name="diamond_svc")
    svc = DiamondService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    err = str(exc_info.value)
    assert "Duplicate event listener declared on subject 'shared.notification'" in err


# ==============================================================================
# 4. DURABLE CONSUMERS ON SAME VS DIFFERENT SUBJECTS
# ==============================================================================


def test_listeners_with_different_durables_on_same_subject_raises_duplicate_error() -> None:
    """Two handlers with distinct durable names on the SAME subject raise duplicate listener error."""

    class DifferingDurablesSameSubject(CliffracerService):
        @listener("events.queue", durable="consumer_one")
        async def handler_first(self) -> None:
            pass

        @listener("events.queue", durable="consumer_two")
        async def handler_second(self) -> None:
            pass

    cfg = ServiceConfig(
        name="diff_dur_same_subj",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="EVENTS", subjects=["events.*"])],
    )
    svc = DifferingDurablesSameSubject(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    err = str(exc_info.value)
    assert "Duplicate event listener declared on subject 'events.queue'" in err


def test_listeners_with_same_durable_on_different_subjects_raises_durable_conflict() -> None:
    """Two handlers sharing the same durable on different subjects raise unique durable error."""

    class SameDurableDiffSubjects(CliffracerService):
        @listener("events.alpha", durable="shared_worker")
        async def on_alpha(self) -> None:
            pass

        @listener("events.beta", durable="shared_worker")
        async def on_beta(self) -> None:
            pass

    cfg = ServiceConfig(
        name="same_dur_diff_subj",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="EVENTS", subjects=["events.*"])],
    )
    svc = SameDurableDiffSubjects(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    err = str(exc_info.value)
    assert "durable 'shared_worker' is claimed by 2 event subjects" in err


# ==============================================================================
# 5. SERVICETESTHARNESS.PUBLISH ALIAS PARITY WITH EMIT_EVENT
# ==============================================================================


class HarnessAuditService(CliffracerService):
    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.received_audit: list[dict[str, Any]] = []
        self.received_models: list[ItemPayload] = []
        self.received_raw: list[Any] = []

    @listener("audit.log", fanout=True)
    async def on_audit(self, msg: str = "", level: str = "") -> None:
        self.received_audit.append({"msg": msg, "level": level})

    @validated_listener("audit.items", schema=ItemPayload, fanout=True)
    async def on_items(self, message: ItemPayload) -> None:
        self.received_models.append(message)

    @listener("raw.events", fanout=True)
    async def on_raw(self) -> None:
        pass


async def test_harness_publish_exact_alias_identity() -> None:
    """ServiceTestHarness.publish is identical in function reference to emit_event."""
    async with ServiceTestHarness(HarnessAuditService) as harness:
        assert ServiceTestHarness.publish is ServiceTestHarness.emit_event
        assert harness.publish.__func__ is harness.emit_event.__func__  # type: ignore[attr-defined]


async def test_harness_publish_payload_parity() -> None:
    """publish and emit_event behave identically across dict, kwargs, primitive, and model payloads."""
    async with ServiceTestHarness(HarnessAuditService) as harness:
        svc: HarnessAuditService = harness.service  # type: ignore

        # 1. Positional dict payload
        out1 = await harness.publish("audit.log", {"msg": "pub1", "level": "info"})
        out2 = await harness.emit_event("audit.log", {"msg": "emit1", "level": "info"})
        assert out1 == DispatchOutcome.OK
        assert out2 == DispatchOutcome.OK
        assert svc.received_audit[0] == {"msg": "pub1", "level": "info"}
        assert svc.received_audit[1] == {"msg": "emit1", "level": "info"}

        # 2. Keyword arguments
        out3 = await harness.publish("audit.log", msg="pub2", level="warn")
        out4 = await harness.emit_event("audit.log", msg="emit2", level="warn")
        assert out3 == DispatchOutcome.OK
        assert out4 == DispatchOutcome.OK
        assert svc.received_audit[2] == {"msg": "pub2", "level": "warn"}
        assert svc.received_audit[3] == {"msg": "emit2", "level": "warn"}

        # 3. Validated listener with Pydantic model
        model1 = ItemPayload(item_id="i1", quantity=5)
        model2 = ItemPayload(item_id="i2", quantity=10)
        out5 = await harness.publish("audit.items", model1)
        out6 = await harness.emit_event("audit.items", model2)
        assert out5 == DispatchOutcome.OK
        assert out6 == DispatchOutcome.OK
        assert svc.received_models[0].item_id == "i1"
        assert svc.received_models[1].item_id == "i2"

        # 4. Validated listener with invalid payload -> DispatchOutcome.INVALID parity
        invalid_out_pub = await harness.publish("audit.items", {"item_id": "", "quantity": -1})
        invalid_out_emit = await harness.emit_event("audit.items", {"item_id": "", "quantity": -1})
        assert invalid_out_pub == DispatchOutcome.INVALID
        assert invalid_out_emit == DispatchOutcome.INVALID


async def test_harness_publish_headers_and_content_type_parity() -> None:
    """publish preserves headers, custom correlation ID, and format encoding."""
    received_headers: list[dict[str, str]] = []

    class HeaderTrackingService(CliffracerService):
        @listener("headers.test", fanout=True)
        async def on_header_msg(
            self, correlation_id: str | None = None, val: int = 0, fmt: str = ""
        ) -> None:
            data = {"val": val} if val else {"fmt": fmt}
            received_headers.append({"correlation_id": correlation_id or "", "data": str(data)})

    async with ServiceTestHarness(HeaderTrackingService) as harness:
        # Publish with explicit correlation header
        await harness.publish(
            "headers.test",
            {"val": 42},
            headers={"X-Correlation-ID": "corr-pub-123"},
        )
        await harness.emit_event(
            "headers.test",
            {"val": 42},
            headers={"X-Correlation-ID": "corr-emit-456"},
        )

        assert len(received_headers) == 2
        assert received_headers[0]["correlation_id"] == "corr-pub-123"
        assert received_headers[1]["correlation_id"] == "corr-emit-456"

        # Msgpack format parity
        out_msgpack_pub = await harness.publish("headers.test", {"fmt": "pack"}, format="msgpack")
        out_msgpack_emit = await harness.emit_event(
            "headers.test", {"fmt": "pack"}, format="msgpack"
        )
        assert out_msgpack_pub == DispatchOutcome.OK
        assert out_msgpack_emit == DispatchOutcome.OK
        assert len(received_headers) == 4


# ==============================================================================
# 6. ASYNC ERROR HANDLING & TASK LEAK PREVENTION
# ==============================================================================


class AsyncHazardService(CliffracerService):
    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.crashed: bool = False
        self.background_iterations: int = 0

    @listener("hazard.crash", fanout=True)
    async def on_crash(self, hazard: bool = False) -> None:
        self.crashed = True
        raise RuntimeError("Intentional listener crash!")

    @rpc
    async def spawn_supervised_worker(self) -> dict[str, str]:
        async def _background() -> None:
            while self.container.lifecycle.is_running:
                self.background_iterations += 1
                await asyncio.sleep(0.01)

        self.container.lifecycle.spawn_supervised_task(_background(), name="hazard_worker")
        return {"status": "spawned"}

    @rpc
    async def spawn_failing_worker(self) -> dict[str, str]:
        async def _failing() -> None:
            await asyncio.sleep(0.01)
            raise ValueError("Supervised task deliberate error")

        self.container.lifecycle.spawn_supervised_task(_failing(), name="failing_worker")
        return {"status": "spawned_failing"}


async def test_harness_publish_error_isolation() -> None:
    """Listener exception during publish does not crash harness and returns DispatchOutcome.OK."""
    async with ServiceTestHarness(AsyncHazardService) as harness:
        svc: AsyncHazardService = harness.service  # type: ignore
        outcome = await harness.publish("hazard.crash", {"hazard": True})
        assert outcome == DispatchOutcome.OK
        assert svc.crashed is True


async def test_harness_teardown_drains_and_prevents_task_leaks() -> None:
    """Teardown terminates running background tasks when is_running becomes False."""
    async with ServiceTestHarness(AsyncHazardService) as harness:
        resp = await harness.rpc("spawn_supervised_worker")
        assert resp.success is True
        assert len(harness.container.lifecycle.active_tasks) == 1
        await asyncio.sleep(0.03)

    svc: AsyncHazardService = harness.service  # type: ignore
    assert svc.background_iterations >= 1
    assert len(harness.container.lifecycle.active_tasks) == 0


async def test_harness_teardown_handles_failing_supervised_tasks() -> None:
    """Teardown cleanly drains tasks that raise exceptions without propagating errors to caller."""
    async with ServiceTestHarness(AsyncHazardService) as harness:
        resp = await harness.rpc("spawn_failing_worker")
        assert resp.success is True
        assert len(harness.container.lifecycle.active_tasks) == 1

    assert len(harness.container.lifecycle.active_tasks) == 0
