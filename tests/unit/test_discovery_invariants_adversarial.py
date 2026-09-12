"""Adversarial stress-tests for HandlerDiscovery invariant enforcement.

Empirically verifies:
1. Exclusive fanout vs durable invariants across JetStream states.
2. Duplicate durable collision detection, subject overwrite vulnerabilities,
   and cross-namespace isolation.
3. Pull consumer prerequisites (durable existence, fanout exclusion, JetStream requirement).
4. JetStream DLQ stream coverage, wildcard pattern matching, and namespacing alignment.
"""

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig, broadcast, listener
from cliffracer.core.decorators import validated_listener
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec

pytestmark = pytest.mark.unit


class OrderPayload(BaseModel):
    order_id: str
    amount: float


# ==============================================================================
# 1. FANOUT VS DURABLE INVARIANT ENFORCEMENT
# ==============================================================================


def test_fanout_and_durable_conflict_with_jetstream_enabled_raises() -> None:
    """Listener specifying both fanout=True and durable raises ConfigurationError when JetStream is on."""

    class ConflictService(CliffracerService):
        @listener("orders.created", durable="order_worker", fanout=True)
        async def on_order(self) -> None:
            pass

    cfg = ServiceConfig(
        name="conflict_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = ConflictService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    msg = str(exc_info.value)
    assert "declare(s) BOTH a durable and fanout=True" in msg
    assert "orders.created" in msg


def test_fanout_and_durable_conflict_with_jetstream_disabled_is_permitted() -> None:
    """When jetstream_enabled=False, fanout=True + durable is permitted per commit 7b6da13."""

    class ConflictDisabledService(CliffracerService):
        @listener("orders.created", durable="order_worker", fanout=True)
        async def on_order(self) -> None:
            pass

    cfg = ServiceConfig(
        name="conflict_disabled_svc",
        jetstream_enabled=False,
    )
    svc = ConflictDisabledService(cfg)
    reg = HandlerDiscovery.discover(svc, cfg)
    assert "orders.created" in reg.event_fanout


def test_validated_listener_fanout_and_durable_conflict_raises() -> None:
    """Validated listener specifying both fanout=True and durable raises ConfigurationError."""

    class ValidatedConflictService(CliffracerService):
        @validated_listener(
            "orders.validated",
            schema=OrderPayload,
            durable="val_worker",
            fanout=True,
        )
        async def on_validated(self, order: OrderPayload) -> None:
            pass

    cfg = ServiceConfig(
        name="val_conflict_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = ValidatedConflictService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    assert "declare(s) BOTH a durable and fanout=True" in str(exc_info.value)


def test_broadcast_and_durable_on_same_subject_conflict_raises() -> None:
    """Declaring @broadcast and @listener(durable=...) on the same subject conflicts and raises."""

    class BroadcastDurableConflictService(CliffracerService):
        @broadcast("alerts.general")
        async def on_broadcast(self) -> None:
            pass

        @listener("alerts.general", durable="alert_durable")
        async def on_listener(self) -> None:
            pass

    cfg = ServiceConfig(
        name="bc_conflict_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = BroadcastDurableConflictService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    assert "declare(s) BOTH a durable and fanout=True" in str(
        exc_info.value
    ) or "Duplicate event listener declared on subject 'alerts.general'" in str(exc_info.value)


def test_unspecified_listener_semantics_raises_configuration_error() -> None:
    """Listener declaring neither fanout=True nor durable name raises ConfigurationError."""

    class NakedListenerService(CliffracerService):
        @listener("events.naked")
        async def on_event(self) -> None:
            pass

    cfg = ServiceConfig(name="naked_svc", jetstream_enabled=True)
    svc = NakedListenerService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    msg = str(exc_info.value)
    assert "declare neither a durable nor fanout" in msg
    assert "events.naked" in msg


def test_durable_with_jetstream_disabled_raises_inert_configuration_error() -> None:
    """Listener declaring durable with jetstream_enabled=False raises ConfigurationError."""

    class InertDurableService(CliffracerService):
        @listener("events.inert", durable="my_durable")
        async def on_inert(self) -> None:
            pass

    cfg = ServiceConfig(name="inert_svc", jetstream_enabled=False)
    svc = InertDurableService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    msg = str(exc_info.value)
    assert "makes inert" in msg
    assert "events.inert" in msg


# ==============================================================================
# 2. DUPLICATE DURABLE COLLISION INVARIANTS
# ==============================================================================


def test_duplicate_durable_across_different_subjects_raises() -> None:
    """Two handlers on distinct subjects sharing the same durable name raise ConfigurationError."""

    class DuplicateDurableDiffSubjects(CliffracerService):
        @listener("orders.created", durable="shared_consumer")
        async def on_created(self) -> None:
            pass

        @listener("orders.shipped", durable="shared_consumer")
        async def on_shipped(self) -> None:
            pass

    cfg = ServiceConfig(
        name="dup_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = DuplicateDurableDiffSubjects(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    msg = str(exc_info.value)
    assert "durable 'shared_consumer' is claimed by 2 event subjects" in msg
    assert "orders.created" in msg
    assert "orders.shipped" in msg


def test_duplicate_listener_on_same_subject_raises_configuration_error() -> None:
    """Multiple handlers on the SAME subject raise ConfigurationError."""

    class DuplicateSameSubjectService(CliffracerService):
        @listener("orders.created", durable="shared_consumer")
        async def handler_alpha(self) -> None:
            pass

        @listener("orders.created", durable="shared_consumer")
        async def handler_beta(self) -> None:
            pass

    cfg = ServiceConfig(
        name="same_subject_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = DuplicateSameSubjectService(cfg)

    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    msg = str(exc_info.value)
    assert "Duplicate event listener declared on subject 'orders.created'" in msg


def test_duplicate_durable_with_cross_namespace_subject_collision_raises() -> None:
    """Cross-namespace subject and namespaced subject sharing a durable raise ConfigurationError."""

    class CrossNamespaceDurableConflictService(CliffracerService):
        @listener("events.ping", durable="global_ping_worker", cross_namespace=True)
        async def on_cross(self) -> None:
            pass

        @listener("events.ping", durable="global_ping_worker", cross_namespace=False)
        async def on_local(self) -> None:
            pass

    cfg = ServiceConfig(
        name="cross_dup_svc",
        namespace="billing",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = CrossNamespaceDurableConflictService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    msg = str(exc_info.value)
    assert "durable 'global_ping_worker' is claimed by 2 event subjects" in msg
    assert "*.events.ping" in msg
    assert "billing.events.ping" in msg


def test_duplicate_durable_between_standard_and_validated_listener_raises() -> None:
    """Standard listener and validated listener sharing a durable raise ConfigurationError."""

    class MixedListenerDurableConflict(CliffracerService):
        @listener("events.raw", durable="shared_worker")
        async def on_raw(self) -> None:
            pass

        @validated_listener("events.typed", schema=OrderPayload, durable="shared_worker")
        async def on_typed(self, data: OrderPayload) -> None:
            pass

    cfg = ServiceConfig(
        name="mixed_dup_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = MixedListenerDurableConflict(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    assert "durable 'shared_worker' is claimed by 2 event subjects" in str(exc_info.value)


# ==============================================================================
# 3. PULL CONSUMER INVARIANTS AND STREAM MATCHING
# ==============================================================================


def test_pull_consumer_without_durable_raises() -> None:
    """Pull consumer with pull=True but no durable raises ConfigurationError."""

    class PullNoDurableService(CliffracerService):
        @listener("events.pull", pull=True)
        async def on_pull(self) -> None:
            pass

    cfg = ServiceConfig(
        name="pull_no_dur_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = PullNoDurableService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    assert "declares pull=True with no durable" in str(exc_info.value)


def test_pull_consumer_with_fanout_raises() -> None:
    """Pull consumer declaring both pull=True and fanout=True raises ConfigurationError."""

    class PullFanoutService(CliffracerService):
        @listener("events.pull", durable="pull_dur", pull=True, fanout=True)
        async def on_pull(self) -> None:
            pass

    cfg = ServiceConfig(
        name="pull_fanout_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = PullFanoutService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    assert "declares both pull=True and fanout=True" in str(exc_info.value)


def test_pull_consumer_with_jetstream_disabled_raises() -> None:
    """Pull consumer with jetstream_enabled=False raises ConfigurationError."""

    class PullDisabledService(CliffracerService):
        @listener("events.pull", durable="pull_dur", pull=True)
        async def on_pull(self) -> None:
            pass

    cfg = ServiceConfig(name="pull_disabled_svc", jetstream_enabled=False)
    svc = PullDisabledService(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    assert "declares pull=True but this service has jetstream_enabled=False" in str(exc_info.value)


def test_pull_consumer_subject_not_validated_against_stream_specs_gap() -> None:
    """ARCHITECTURAL GAP: HandlerDiscovery does NOT assert pull consumer subjects match declared streams.

    Unlike DLQ coverage (which asserts subject_covered_by(config.jetstream_streams, dlq_subject)),
    pull consumers (and push durable consumers) do NOT have their subjects validated
    against config.jetstream_streams during discovery. This permits startup with
    uncovered pull consumers if another service owns the stream, but causes runtime
    failures if the stream does not exist.
    """

    class PullUncoveredStreamService(CliffracerService):
        @listener("uncovered.pull.event", durable="pull_worker", pull=True)
        async def on_pull(self) -> None:
            pass

    cfg = ServiceConfig(
        name="pull_uncovered_svc",
        jetstream_enabled=True,
        # Only DLQ stream is declared, 'uncovered.pull.event' is NOT claimed by any stream
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = PullUncoveredStreamService(cfg)

    # Discovery passes without validating that 'uncovered.pull.event' belongs to any declared stream
    reg = HandlerDiscovery.discover(svc, cfg)
    assert "uncovered.pull.event" in reg.event_pull
    assert reg.event_durables.get("uncovered.pull.event") == "pull_worker"


# ==============================================================================
# 4. DLQ STREAM COVERAGE AND ALIGNMENT
# ==============================================================================


def test_dlq_coverage_missing_raises_stream_declaration_error() -> None:
    """Active JetStream configuration without stream coverage for DLQ subject raises StreamDeclarationError."""
    cfg = ServiceConfig(
        name="no_dlq_stream_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.*"])],
        dlq_subject="dlq.no_dlq_stream_svc",
    )
    with pytest.raises(StreamDeclarationError) as exc_info:
        HandlerDiscovery.validate_dlq_coverage(cfg)

    msg = str(exc_info.value)
    assert "no declared stream covers the dead-letter subject 'dlq.no_dlq_stream_svc'" in msg
    assert "orders.*" in msg


def test_dlq_coverage_with_wildcard_stream_matches() -> None:
    """DLQ subject is covered by root 'dlq.*' or 'dlq.>' stream."""
    for pattern in ["dlq.*", "dlq.>"]:
        cfg = ServiceConfig(
            name="orders_svc",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="DLQ_STREAM", subjects=[pattern])],
            dlq_subject="dlq.orders_svc",
        )
        # Should succeed without error
        HandlerDiscovery.validate_dlq_coverage(cfg)


def test_dlq_coverage_namespaced_template_alignment() -> None:
    """DLQ template with {namespace} correctly matches namespaced stream, rejects mismatched stream."""
    # Matched case
    cfg_matched = ServiceConfig(
        name="orders_svc",
        namespace="prod",
        dlq_subject="{namespace}.dlq.{service}",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="PROD_DLQ", subjects=["prod.dlq.*"])],
    )
    HandlerDiscovery.validate_dlq_coverage(cfg_matched)

    # Mismatched case: stream claims 'dlq.*', but DLQ subject resolves to 'prod.dlq.orders_svc'
    cfg_mismatched = ServiceConfig(
        name="orders_svc",
        namespace="prod",
        dlq_subject="{namespace}.dlq.{service}",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="ROOT_DLQ", subjects=["dlq.*"])],
    )
    with pytest.raises(StreamDeclarationError):
        HandlerDiscovery.validate_dlq_coverage(cfg_mismatched)


def test_dlq_coverage_skipped_when_jetstream_disabled() -> None:
    """When jetstream_enabled=False, validate_dlq_coverage is a no-op even with empty streams."""
    cfg = ServiceConfig(
        name="disabled_js_svc",
        jetstream_enabled=False,
        jetstream_streams=[],
        dlq_subject="dlq.disabled_js_svc",
    )
    # Does not raise
    HandlerDiscovery.validate_dlq_coverage(cfg)


def test_validate_subject_type_refuses_non_string_subjects() -> None:
    """HandlerDiscovery._validate_subject_type raises TypeError if a non-string subject is supplied."""
    cfg = ServiceConfig(name="test_svc")

    class WeirdSubjectService(CliffracerService):
        pass

    svc = WeirdSubjectService(cfg)
    with pytest.raises(TypeError, match="event subject must be a str"):
        HandlerDiscovery._validate_subject_type(svc, 12345, lambda: None)
