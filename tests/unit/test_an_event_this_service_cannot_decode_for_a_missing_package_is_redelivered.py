"""An event whose body needs a package this service lacks is redelivered, not terminated.

A JetStream event with a msgpack body, on a service installed without the msgpack extra, was
terminated as a "Decode error" and stored in the dead-letter stream as replacement characters. The
RPC path already calls the same condition the service's own fault. Termination is for a judgement
of the message (ADR-0014), and a missing optional package is a fact about this replica: another,
with the extra, would have processed the message. So it takes the path a handler failure takes. On
JetStream it is naked and redelivered, and dead-lettered at the delivery limit; elsewhere it is
logged as the service's fault and nothing is written to the dead-letter stream.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core import validation
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import MockMessage, ServiceTestHarness
from cliffracer.testing.messages import MockJetStreamMetadata

pytestmark = pytest.mark.unit

MSGPACK_BODY = b"\x81\xa5value\x01"  # msgpack for {"value": 1}
MSGPACK = {"Content-Type": "application/msgpack"}
SUBJECT = "probe.declared.thing"


class Consumer(CliffracerService):
    seen: list[int] = []

    @listener(SUBJECT, durable="prober")
    async def on_thing(self, subject: str, value: int = 0) -> None:
        type(self).seen.append(value)


class CoreConsumer(CliffracerService):
    @listener(SUBJECT, fanout=True)
    async def on_thing(self, subject: str, value: int = 0) -> None:
        Consumer.seen.append(value)


async def deliver(
    harness: ServiceTestHarness, body: bytes, headers: dict[str, str], *, num_delivered: int
) -> MockMessage:
    """One raw JetStream delivery: the harness encodes what it is given, and these are bytes."""
    msg = MockMessage(
        subject=SUBJECT,
        data=body,
        headers=headers,
        metadata=MockJetStreamMetadata(num_delivered=num_delivered),
    )
    await harness.container.dispatcher.jetstream.handle_jetstream_event(msg, pattern=SUBJECT)
    return msg


def _config(*, jetstream: bool = True) -> ServiceConfig:
    return ServiceConfig(
        name="consumer_svc",
        health_port=0,
        jetstream_enabled=jetstream,
        jetstream_streams=[
            StreamSpec(name="DECLARED", subjects=["probe.declared.>"]),
            StreamSpec(name="DLQ", subjects=["dlq.>"]),
        ]
        if jetstream
        else [],
    )


def _dead_letters(harness: ServiceTestHarness) -> list[dict]:
    return [
        json.loads(payload)
        for subject, payload, _ in harness.jetstream.published
        if subject.startswith("dlq.")
    ]


@pytest.fixture(autouse=True)
def _reset():
    Consumer.seen = []


async def test_a_first_delivery_is_naked_and_leaves_no_dead_letter():
    async with ServiceTestHarness(Consumer, config=_config()) as harness:
        with patch.object(validation, "msgpack", None):
            msg = await deliver(harness, MSGPACK_BODY, MSGPACK, num_delivered=1)

        assert msg.nacked and not msg.terminated and not msg.acked
        assert _dead_letters(harness) == []
        assert Consumer.seen == []


async def test_the_delivery_limit_dead_letters_it_and_names_the_package():
    async with ServiceTestHarness(Consumer, config=_config()) as harness:
        with patch.object(validation, "msgpack", None):
            msg = await deliver(
                harness, MSGPACK_BODY, {**MSGPACK, "X-Correlation-ID": "trace-1"}, num_delivered=5
            )

        assert msg.terminated and not msg.nacked
        (record,) = _dead_letters(harness)
        assert "msgpack" in record["error"]
        assert record["correlation_id"] == "trace-1", record


async def test_a_replica_with_the_package_processes_the_same_message():
    pytest.importorskip("msgpack")
    async with ServiceTestHarness(Consumer, config=_config()) as harness:
        msg = await deliver(harness, MSGPACK_BODY, MSGPACK, num_delivered=1)

        assert msg.acked and not msg.nacked and not msg.terminated
        assert Consumer.seen == [1]


async def test_CONTROL_a_body_that_is_not_json_is_still_terminated_at_once():
    async with ServiceTestHarness(Consumer, config=_config()) as harness:
        msg = await deliver(
            harness,
            b"{not json",
            {"Content-Type": "application/json"},
            num_delivered=1,
        )

        assert msg.terminated and not msg.nacked
        (record,) = _dead_letters(harness)
        assert record["error"].startswith("Decode error")


async def test_CONTROL_a_msgpack_body_that_is_not_msgpack_is_still_terminated_at_once():
    pytest.importorskip("msgpack")
    async with ServiceTestHarness(Consumer, config=_config()) as harness:
        msg = await deliver(harness, b"\xc1\xc1\xc1", MSGPACK, num_delivered=1)

        assert msg.terminated and not msg.nacked
        assert len(_dead_letters(harness)) == 1


async def test_without_jetstream_the_event_is_logged_as_the_services_fault_and_not_dead_lettered():
    lines: list[str] = []
    handler = logger.add(lambda m: lines.append(m.record["message"]), level="ERROR")
    try:
        async with ServiceTestHarness(CoreConsumer, config=_config(jetstream=False)) as harness:
            dispatcher = harness.container.dispatcher
            with (
                patch.object(validation, "msgpack", None),
                patch.object(
                    dispatcher.dlq, "dead_letter_decode_error", AsyncMock()
                ) as dead_letter,
            ):
                msg = MockMessage(subject=SUBJECT, data=MSGPACK_BODY, headers=MSGPACK)
                await dispatcher.events.handle_event(msg)

            dead_letter.assert_not_awaited()
            assert Consumer.seen == []
    finally:
        logger.remove(handler)

    assert any("msgpack" in line for line in lines), lines
