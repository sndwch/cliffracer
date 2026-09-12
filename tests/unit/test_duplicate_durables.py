"""Unit tests verifying that distinct event subjects cannot share a durable consumer name."""

from unittest.mock import AsyncMock

import pytest
from nats.js.api import AckPolicy, ConsumerConfig

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig, listener
from cliffracer.core.jetstream import (
    TUNED_CONSUMER_FIELDS,
    consumer_config_drift,
    consumer_config_for,
)

pytestmark = pytest.mark.unit


def _config(**overrides):
    return ServiceConfig(name="svc", namespace="ns", jetstream_enabled=True, **overrides)


def test_two_subjects_sharing_a_durable_are_refused():
    """Multiple event listeners sharing a single durable name are refused."""

    class S(CliffracerService):
        @listener("events.extraction.completed", durable="jorbo-extraction-results")
        async def on_completed(self, subject: str) -> None:
            pass

        @listener("events.extraction.failed", durable="jorbo-extraction-results")
        async def on_failed(self, subject: str) -> None:
            pass

    svc = S(_config())

    with pytest.raises(ConfigurationError) as exc:
        svc._discover_handlers()

    message = str(exc.value)
    assert "jorbo-extraction-results" in message
    assert "ns.events.extraction.completed" in message
    assert "ns.events.extraction.failed" in message


def test_the_refusal_explains_the_silence():
    """Verify error message clearly describes durable name collision."""

    class S(CliffracerService):
        @listener("events.a", durable="shared")
        async def on_a(self, subject: str) -> None:
            pass

        @listener("events.b", durable="shared")
        async def on_b(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config())._discover_handlers()

    message = str(exc.value)
    assert "DLQ" in message
    assert "own durable" in message


def test_distinct_durables_are_fine():
    class S(CliffracerService):
        @listener("events.a", durable="durable-a")
        async def on_a(self, subject: str) -> None:
            pass

        @listener("events.b", durable="durable-b")
        async def on_b(self, subject: str) -> None:
            pass

    svc = S(_config())
    svc._discover_handlers()

    assert svc.container.registry.event_durables == {
        "ns.events.a": "durable-a",
        "ns.events.b": "durable-b",
    }


def test_listeners_without_durables_are_unaffected():
    """Core-NATS fan-out listeners have no durable and cannot collide."""

    class S(CliffracerService):
        @listener("events.a", fanout=True)
        async def on_a(self, subject: str) -> None:
            pass

        @listener("events.b", fanout=True)
        async def on_b(self, subject: str) -> None:
            pass

    svc = S(_config())
    svc._discover_handlers()

    assert svc.container.registry.event_durables == {}


def test_one_handler_listening_to_two_subjects_still_needs_two_durables():
    """Stacked decorators on one method are still two filter subjects."""

    class S(CliffracerService):
        @listener("events.a", durable="shared")
        @listener("events.b", durable="shared")
        async def on_either(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError):
        S(_config())._discover_handlers()


def test_drift_is_empty_when_the_server_agrees():
    asked = consumer_config_for(_config())
    same = ConsumerConfig(
        ack_policy=AckPolicy.EXPLICIT, ack_wait=30.0, max_deliver=5, max_ack_pending=64
    )

    assert consumer_config_drift(asked, same) == []


def test_drift_names_the_field_and_both_values():
    """Verify detected drift reports field name and requested vs existing values."""
    asked = consumer_config_for(_config(jetstream_max_deliver=99, jetstream_ack_wait=1.0))
    existing = ConsumerConfig(
        ack_policy=AckPolicy.EXPLICIT, ack_wait=30.0, max_deliver=5, max_ack_pending=64
    )

    drift = {field: (want, have) for field, want, have in consumer_config_drift(asked, existing)}

    assert drift["max_deliver"] == (99, 5)
    assert drift["ack_wait"] == (1.0, 30.0)
    assert "max_ack_pending" not in drift


def test_drift_covers_every_field_consumer_config_for_sets():
    """Guard against a new tuning field being added and never compared."""
    asked = consumer_config_for(_config())

    for field in TUNED_CONSUMER_FIELDS:
        assert getattr(asked, field) is not None, f"{field} is not set by consumer_config_for"


@pytest.mark.asyncio
async def test_a_drifted_consumer_is_reported_not_swallowed():
    svc = CliffracerService(_config(jetstream_max_deliver=99))
    warnings = []
    svc.logger = type(
        "L", (), {"warning": lambda _s, m: warnings.append(m), "debug": lambda _s, m: None}
    )()

    sub = AsyncMock()
    sub.consumer_info.return_value = type(
        "Info",
        (),
        {
            "stream_name": "EVENTS",
            "config": ConsumerConfig(
                ack_policy=AckPolicy.EXPLICIT, ack_wait=30.0, max_deliver=5, max_ack_pending=64
            ),
        },
    )()

    await svc.container.dispatcher.report_consumer_drift(sub, "some-durable")

    assert len(warnings) == 1
    assert "max_deliver=5" in warnings[0]
    assert "99" in warnings[0]
    assert "nats consumer rm EVENTS some-durable" in warnings[0]


@pytest.mark.asyncio
async def test_an_unreadable_consumer_never_blocks_startup():
    """Reporting config must not be able to stop a service starting."""
    svc = CliffracerService(_config())
    svc.logger = type("L", (), {"warning": lambda _s, m: None, "debug": lambda _s, m: None})()

    sub = AsyncMock()
    sub.consumer_info.side_effect = RuntimeError("broker said no")

    await svc.container.dispatcher.report_consumer_drift(sub, "some-durable")  # must not raise
