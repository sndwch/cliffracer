"""Unit tests verifying that distinct event subjects cannot share a durable consumer name."""

import dataclasses
from unittest.mock import AsyncMock

import pytest
from nats.js.api import AckPolicy, ConsumerConfig

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig, listener
from cliffracer.core.jetstream import (
    TUNED_CONSUMER_FIELDS,
    StreamSpec,
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

    with pytest.raises(ConfigurationError) as raised:
        S(_config())._discover_handlers()

    # The durable-uniqueness check, named: other validators raise ConfigurationError too, and
    # any of them firing first would otherwise keep this green while the one it is named for rotted.
    message = str(raised.value)
    assert "durable 'shared' is claimed by 2 event subjects" in message
    assert "events.a" in message and "events.b" in message


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


def test_drift_compares_exactly_the_fields_consumer_config_for_asks_for(monkeypatch):
    """A field asked for and never compared is a tuning edit the server ignores
    in silence; a field compared and never asked for is drift that cannot occur.

    What is asked for is read from the call `consumer_config_for` makes to build
    the config, not from the config it returns: a returned `ConsumerConfig`
    carries defaults, so a field it stopped passing would still read as set.
    """
    import cliffracer.core.jetstream as jetstream

    asked_for: list[str] = []
    real = jetstream.ConsumerConfig

    def recording(**kwargs):
        asked_for.extend(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(jetstream, "ConsumerConfig", recording)

    consumer_config_for(_config())

    assert sorted(asked_for) == sorted(TUNED_CONSUMER_FIELDS)


@pytest.mark.parametrize("field", TUNED_CONSUMER_FIELDS)
def test_a_difference_in_each_tuned_field_is_reported(field):
    asked = consumer_config_for(_config())
    live = dataclasses.replace(asked)
    changed = {
        "ack_policy": AckPolicy.NONE,
        "ack_wait": asked.ack_wait + 1,
        "max_deliver": asked.max_deliver + 1,
        "max_ack_pending": asked.max_ack_pending + 1,
    }[field]
    setattr(live, field, changed)

    assert [name for name, _, _ in consumer_config_drift(asked, live)] == [field]


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


# --- the report is wired to the subscription that creates the durable -------------------------
#
# The two tests above call `report_consumer_drift` themselves, which holds the reporter's logic
# and nothing about when it runs. These drive the subscription step with a JetStream context
# whose consumer disagrees with the config, and read the warning that comes out.


def _drifted_sub() -> AsyncMock:
    sub = AsyncMock()
    sub.consumer_info.return_value = type(
        "Info",
        (),
        {
            "stream_name": "EVENTS",
            "config": ConsumerConfig(
                ack_policy=AckPolicy.EXPLICIT, ack_wait=30.0, max_deliver=3, max_ack_pending=64
            ),
        },
    )()
    return sub


async def _subscribe_a_durable(pull: bool, sub: AsyncMock) -> list[str]:
    from loguru import logger

    class Svc(CliffracerService):
        @listener("events.thing", durable="things", pull=pull)
        async def on_thing(self, subject: str) -> None:
            pass

    svc = Svc(
        ServiceConfig(
            name="svc",
            jetstream_enabled=True,
            jetstream_streams=[
                StreamSpec(name="EVENTS", subjects=["events.*"]),
                StreamSpec(name="DLQ", subjects=["dlq.>"]),
            ],
            jetstream_max_deliver=9,
        )
    )
    svc._discover_handlers()
    svc.nc = AsyncMock()
    svc.js = AsyncMock()
    svc.js.subscribe.return_value = sub
    svc.js.pull_subscribe.return_value = sub
    svc._running = False  # so the subscription-handler and pull-loop tasks exit at once

    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        await svc.container.setup_subscriptions()
    finally:
        logger.remove(sink)
    return [w for w in warnings if "runs with" in w]


@pytest.mark.asyncio
@pytest.mark.parametrize("pull", [False, True], ids=["push", "pull"])
async def test_creating_a_durable_reports_a_drifted_consumer(pull):
    drift = await _subscribe_a_durable(pull, _drifted_sub())

    (line,) = drift
    assert "max_deliver=3, not the 9 asked for" in line, line
    assert "nats consumer rm EVENTS things" in line, line


@pytest.mark.asyncio
@pytest.mark.parametrize("pull", [False, True], ids=["push", "pull"])
async def test_CONTROL_a_consumer_that_agrees_with_the_config_reports_nothing(pull):
    sub = _drifted_sub()
    sub.consumer_info.return_value.config.max_deliver = 9

    assert await _subscribe_a_durable(pull, sub) == []
