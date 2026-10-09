"""`service.schedules` publishes an event for the broker to write onto its subject later.

The schedule is a stream message on `_sched.<subject>.<key>` with `Nats-Schedule: @at <instant>`
and `Nats-Schedule-Target: <subject>`; the broker (nats-server 2.12 and later) writes the event onto
the target when it is due. These rows read what is sent and what is refused, against the harness's
in-memory JetStream; `scripts/check_message_schedules.py` runs the live rows on a pinned broker.
"""

import json
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.js.errors import APIError

from cliffracer import CliffracerService, ServiceConfig, StreamSpec
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.jetstream import MessageScheduleError
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit

WHEN = datetime(2030, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)


def _config(*streams: StreamSpec, **extra) -> ServiceConfig:
    return ServiceConfig(
        name="remind",
        health_port=0,
        jetstream_enabled=True,
        jetstream_streams=list(streams)
        or [
            StreamSpec(
                name="REMIND",
                subjects=["remind.>", "_sched.remind.>"],
                allow_msg_schedules=True,
            ),
            StreamSpec(name="DLQ", subjects=["dlq.>"]),
        ],
        **extra,
    )


async def _published(config: ServiceConfig, call) -> list[tuple[str, dict, dict]]:
    async with ServiceTestHarness(CliffracerService, config=config) as harness:
        await call(harness.service.schedules)
        return [
            (subject, json.loads(data), dict(headers))
            for subject, data, headers in harness.jetstream.published
        ]


# --- what is sent ----------------------------------------------------------------------------------


async def test_publish_at_sends_the_event_to_its_schedule_subject_with_the_schedule_headers():
    ((subject, body, headers),) = await _published(
        _config(),
        lambda s: s.publish_at("remind.due", when=WHEN, key="r42", order_id="o-1"),
    )

    assert subject == "_sched.remind.due.r42"
    assert headers["Nats-Schedule"] == "@at 2030-01-02T03:04:05.678Z"
    assert headers["Nats-Schedule-Target"] == "remind.due"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Correlation-ID"] == body["correlation_id"]
    assert body["data"] == {"order_id": "o-1"} and body["source_service"] == "remind"


async def test_a_when_in_another_zone_is_sent_as_the_same_instant_in_utc():
    later = WHEN.astimezone(timezone(timedelta(hours=-5)))
    ((_, _, headers),) = await _published(
        _config(), lambda s: s.publish_at("remind.due", when=later, key="r42")
    )

    assert headers["Nats-Schedule"] == "@at 2030-01-02T03:04:05.678Z"


async def test_publish_in_schedules_after_a_delay_from_now():
    before = datetime.now(UTC)
    ((_, _, headers),) = await _published(
        _config(), lambda s: s.publish_in("remind.due", after=timedelta(hours=1), key="r42")
    )
    after = datetime.now(UTC)

    scheduled = datetime.fromisoformat(headers["Nats-Schedule"].removeprefix("@at "))
    assert before + timedelta(hours=1) - timedelta(milliseconds=1) <= scheduled
    assert scheduled <= after + timedelta(hours=1)


async def test_the_schedule_subject_is_namespaced_and_prefixed_as_its_target_is():
    config = _config(
        StreamSpec(
            name="REMIND",
            subjects=["ops.remind.>", "ops._sched.remind.>"],
            allow_msg_schedules=True,
        ),
        StreamSpec(name="DLQ", subjects=["dlq.>"]),
        namespace="ops",
        subject_prefix="stage",
    )
    ((subject, _, headers),) = await _published(
        config, lambda s: s.publish_at("remind.due", when=WHEN, key="r42")
    )

    assert subject == "stage.ops._sched.remind.due.r42"
    assert headers["Nats-Schedule-Target"] == "stage.ops.remind.due"


async def test_an_idempotency_key_deduplicates_the_scheduling_publish():
    ((_, _, headers),) = await _published(
        _config(),
        lambda s: s.publish_at("remind.due", when=WHEN, key="r42", idempotency_key="call-1"),
    )

    assert headers["Nats-Msg-Id"] == "_sched.remind.due.r42:call-1"


async def test_cancel_purges_the_schedule_subject_from_its_stream():
    async with ServiceTestHarness(CliffracerService, config=_config()) as harness:
        purge = AsyncMock(return_value=True)
        harness.jetstream.purge_stream = purge  # type: ignore[method-assign]
        await harness.service.schedules.cancel("remind.due", key="r42")

    purge.assert_awaited_once_with("REMIND", subject="_sched.remind.due.r42")


# --- what is refused before anything is sent ------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "error", "match"),
    [
        (
            lambda s: s.publish_at("remind.due", when=datetime(2030, 1, 1), key="r"),
            TypeError,
            "aware datetime",
        ),
        (
            lambda s: s.publish_in("remind.due", after=timedelta(seconds=-1), key="r"),
            ValueError,
            "zero or more",
        ),
        (
            lambda s: s.publish_at("remind.due", when=WHEN, key="a.b"),
            ValueError,
            "one subject token",
        ),
        (lambda s: s.publish_at("remind.due", when=WHEN, key=""), ValueError, "one subject token"),
        (
            lambda s: s.publish_at("remind.due", when=WHEN, key="a b"),
            ValueError,
            "one subject token",
        ),
        (
            lambda s: s.publish_at("other.due", when=WHEN, key="r"),
            MessageScheduleError,
            "no declared stream covers 'other.due'",
        ),
    ],
    ids=["naive-when", "negative-after", "dotted-key", "empty-key", "spaced-key", "no-stream"],
)
async def test_a_schedule_that_cannot_be_made_is_refused_and_nothing_is_sent(call, error, match):
    async with ServiceTestHarness(CliffracerService, config=_config()) as harness:
        with pytest.raises(error, match=match):
            await call(harness.service.schedules)
        assert harness.jetstream.published == []


async def test_a_stream_that_does_not_allow_schedules_is_refused_by_name():
    config = _config(
        StreamSpec(name="REMIND", subjects=["remind.>", "_sched.remind.>"]),
        StreamSpec(name="DLQ", subjects=["dlq.>"]),
    )
    async with ServiceTestHarness(CliffracerService, config=config) as harness:
        with pytest.raises(MessageScheduleError, match="'REMIND' does not allow message schedules"):
            await harness.service.schedules.publish_at("remind.due", when=WHEN, key="r")


async def test_a_schedule_in_another_stream_than_its_target_is_refused():
    config = _config(
        StreamSpec(name="REMIND", subjects=["remind.>"]),
        StreamSpec(name="SCHED", subjects=["_sched.remind.>"], allow_msg_schedules=True),
        StreamSpec(name="DLQ", subjects=["dlq.>"]),
    )
    async with ServiceTestHarness(CliffracerService, config=config) as harness:
        with pytest.raises(MessageScheduleError, match="only within the stream that holds"):
            await harness.service.schedules.publish_at("remind.due", when=WHEN, key="r")


async def test_without_jetstream_a_schedule_is_refused():
    config = ServiceConfig(name="remind", health_port=0)
    async with ServiceTestHarness(CliffracerService, config=config) as harness:
        with pytest.raises(ConfigurationError, match="needs jetstream_enabled"):
            await harness.service.schedules.publish_at("remind.due", when=WHEN, key="r")


async def test_a_broker_before_2_12_is_refused_naming_the_floor():
    async with ServiceTestHarness(CliffracerService, config=_config()) as harness:
        harness.service.nc.connected_server_version = SimpleNamespace(major=2, minor=11)
        with pytest.raises(
            MessageScheduleError, match="nats-server 2.11, and message schedules need 2.12"
        ):
            await harness.service.schedules.publish_at("remind.due", when=WHEN, key="r")
        assert harness.jetstream.published == []


@pytest.mark.parametrize(
    ("err_code", "says"),
    [
        (10188, "does not allow message schedules"),
        (10189, "could not read the schedule's time"),
        (10190, "not in the same stream"),
    ],
)
async def test_a_refusal_the_server_makes_is_raised_by_its_meaning(err_code, says):
    async with ServiceTestHarness(CliffracerService, config=_config()) as harness:
        harness.jetstream.publish = AsyncMock(  # type: ignore[method-assign]
            side_effect=APIError(code=400, err_code=err_code, description="refused")
        )
        with pytest.raises(MessageScheduleError, match=says) as caught:
            await harness.service.schedules.publish_at("remind.due", when=WHEN, key="r")

    assert caught.value.details == {"err_code": err_code}


async def test_CONTROL_another_server_error_is_not_read_as_a_schedule_refusal():
    async with ServiceTestHarness(CliffracerService, config=_config()) as harness:
        harness.jetstream.publish = AsyncMock(  # type: ignore[method-assign]
            side_effect=APIError(code=503, err_code=10008, description="unavailable")
        )
        with pytest.raises(APIError):
            await harness.service.schedules.publish_at("remind.due", when=WHEN, key="r")


# --- the declaration -------------------------------------------------------------------------------


def test_a_stream_that_allows_schedules_asks_the_server_for_them():
    spec = StreamSpec(name="R", subjects=["r.>", "_sched.r.>"], allow_msg_schedules=True)
    assert spec.to_stream_config().allow_msg_schedules is True


def test_a_stream_that_does_not_allow_them_does_not_send_the_field():
    assert StreamSpec(name="R", subjects=["r.>"]).to_stream_config().allow_msg_schedules is None


def test_a_stream_that_allows_schedules_must_declare_a_place_for_them():
    with pytest.raises(ValueError, match="declares no subject with a '_sched' token"):
        StreamSpec(name="R", subjects=["r.>"], allow_msg_schedules=True)


def test_a_broker_that_holds_no_schedules_is_a_difference_only_when_they_were_asked_for():
    asked = StreamSpec(name="R", subjects=["r.>", "_sched.r.>"], allow_msg_schedules=True)
    held = asked.to_stream_config()
    ignored = held.evolve(allow_msg_schedules=None)
    unasked = StreamSpec(name="R", subjects=["r.>", "_sched.r.>"])

    assert asked.declared_differences(held) == []
    assert asked.declared_differences(ignored) == [("allow_msg_schedules", True, False)]
    assert unasked.declared_differences(held) == []
