"""Adversarial stress-tests for HandlerDiscovery invariant enforcement.

Empirically verifies:
1. Exclusive fanout vs durable invariants across JetStream states.
2. Duplicate durable collision detection, subject overwrite vulnerabilities,
   and cross-namespace isolation.
3. Pull consumer prerequisites (durable existence, fanout exclusion, JetStream requirement).
4. JetStream DLQ stream coverage, wildcard pattern matching, and namespacing alignment.
"""

from unittest.mock import AsyncMock

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
    """When jetstream_enabled=False, fanout=True with a durable is permitted: the durable
    is inert without JetStream, so it describes intent rather than a subscription."""

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


def _broadcast_and_durable_service(first: str, second: str) -> type[CliffracerService]:
    """Both handlers claim 'alerts.general'. Members are visited by name, so `first` runs first."""
    handlers = {
        "broadcast": (broadcast("alerts.general"), "a broadcast"),
        "listener": (listener("alerts.general", durable="alert_durable"), "a durable listener"),
    }

    async def first_handler(self) -> None:
        pass

    async def second_handler(self) -> None:
        pass

    namespace = {
        f"a_{first}": handlers[first][0](first_handler),
        f"b_{second}": handlers[second][0](second_handler),
    }
    return type("BroadcastDurableConflictService", (CliffracerService,), namespace)


@pytest.mark.parametrize(
    ("first", "second"),
    [("broadcast", "listener"), ("listener", "broadcast")],
    ids=["broadcast-then-listener", "listener-then-broadcast"],
)
def test_broadcast_and_durable_on_same_subject_conflict_raises(first, second) -> None:
    """A @broadcast and a @listener(durable=...) on one subject conflict: whichever is
    discovered second is refused as a duplicate of the subject, naming both handlers.

    That is the refusal that fires; it comes before the durable-versus-fanout invariant is
    reached, so the message must be this one and not either of two.
    """
    service_class = _broadcast_and_durable_service(first, second)
    cfg = ServiceConfig(
        name="bc_conflict_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = service_class(cfg)
    with pytest.raises(ConfigurationError) as exc_info:
        HandlerDiscovery.discover(svc, cfg)

    msg = str(exc_info.value)
    assert (
        f"Duplicate event listener declared on subject 'alerts.general': "
        f"'b_{second}' conflicts with 'a_{first}'"
    ) in msg


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


def test_discovery_alone_does_not_check_a_consumers_subject_against_the_declared_streams() -> None:
    """`discover` scans the class and does not judge stream coverage: it accepts a durable (here
    a pull consumer) whose subject no declared stream carries.

    Startup does judge it: `Container._discover_for_startup` calls `validate_durable_coverage`,
    which refuses an uncovered durable listener in either resource mode (pinned by
    `test_a_durable_listener_with_no_covering_stream_is_refused_at_startup.py`). What `provision`
    mode leaves to the server is the binding of the consumer to a stream, and `bind` mode
    resolves the stream from the declared ones and refuses a subject none claims: the two tests
    below pin those.
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

    reg = HandlerDiscovery.discover(svc, cfg)
    assert "uncovered.pull.event" in reg.event_pull
    assert reg.event_durables.get("uncovered.pull.event") == "pull_worker"


def _uncovered_pull_service(mode: str):
    class UncoveredPull(CliffracerService):
        @listener("uncovered.pull.event", durable="pull_worker", pull=True)
        async def on_pull(self) -> None:
            pass

    cfg = ServiceConfig(
        name="pull_uncovered_svc",
        jetstream_enabled=True,
        jetstream_resource_mode=mode,
        jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
    )
    svc = UncoveredPull(cfg)
    svc.container.js = AsyncMock()
    return svc


async def test_bind_mode_refuses_a_consumer_subject_no_declared_stream_claims() -> None:
    svc = _uncovered_pull_service("bind")

    with pytest.raises(StreamDeclarationError, match="needs exactly one declared stream"):
        await svc.container._bound_consumer_for("uncovered.pull.event", "pull_worker", pull=True)


async def test_CONTROL_provision_mode_leaves_the_consumer_to_the_server() -> None:
    svc = _uncovered_pull_service("provision")

    assert (
        await svc.container._bound_consumer_for("uncovered.pull.event", "pull_worker", pull=True)
        is None
    )


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


def test_the_decorators_refuse_a_non_string_subject_when_the_class_is_defined() -> None:
    """The check that protects a real service: a decorator refuses a non-str subject at once.

    `_validate_subject_type` in discovery is a backstop behind these, reachable only
    through a marker written by hand (next test).
    """
    for decorator, args in (
        (listener, (12345,)),
        (broadcast, (12345,)),
        (validated_listener, (12345, _SubjectModel)),
    ):
        with pytest.raises(ConfigurationError, match="takes a NATS subject string, not int"):
            decorator(*args)


class _SubjectModel(BaseModel):
    value: int


@pytest.mark.parametrize(
    ("marker", "value"),
    [
        ("_cliffracer_events", [12345]),
        ("_cliffracer_validated_events", [(12345, _SubjectModel, None)]),
        ("_cliffracer_broadcast", 12345),
    ],
    ids=["listener", "validated_listener", "broadcast"],
)
def test_discovery_refuses_a_non_string_subject_in_a_marker_written_by_hand(marker, value) -> None:
    """Each of the three discovery sites checks its marker's subject, through `discover`.

    Calling `_validate_subject_type` directly stays green with those call sites
    deleted, so this goes in by the route a service takes.
    """

    class WeirdSubjectService(CliffracerService):
        async def handler(self, message: _SubjectModel) -> None:
            pass

    setattr(WeirdSubjectService.handler, marker, value)
    svc = WeirdSubjectService(ServiceConfig(name="test_svc"))

    with pytest.raises(TypeError, match="event subject must be a str") as caught:
        svc._discover_handlers()
    assert "handler" in str(caught.value)


def test_a_validated_listener_cannot_ask_for_pull_so_discovery_has_nothing_to_ignore():
    """Tripwire for the day `validated_listener` gains `pull=`.

    The validated branch of `HandlerDiscovery.discover` reads the cross-namespace, durable and
    fanout markers but not the pull marker, which is harmless only while the decorator cannot set
    one. Adding the parameter without teaching that branch to read it would bind a pull listener
    as a push subscription, every replica handling every message, which is the outcome
    `validate_pull_is_usable` exists to prevent. If this fails, read `_cliffracer_event_pull` in
    the validated branch and add a test that a validated pull listener without a durable is
    refused, then delete this one.
    """
    import inspect

    assert "pull" not in inspect.signature(validated_listener).parameters


def test_a_validated_listener_is_registered_with_its_event_spec():
    class Ev(BaseModel):
        n: int

    class Svc(CliffracerService):
        @validated_listener("v.x", Ev, fanout=True)
        async def on_v(self, ev: Ev) -> None: ...

    svc = Svc(ServiceConfig(name="v_svc", health_port=0))
    svc._discover_handlers()
    registry = svc.container.registry

    assert "v.x" in registry.event_specs_by_subject
