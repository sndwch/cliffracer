"""`MockMessage.metadata` answers as a real message does, and its sequence has the real shape.

`nats.aio.msg.Msg.metadata` raises `NotJSMessageError` for a message whose reply is not a JetStream ack
subject; `MockMessage.metadata` answered `None`, so code that read it with a plain `getattr` default
worked under the double and failed against the real message. `MockJetStreamMetadata.sequence` was an
`int` where a real delivery's is a `SequencePair(consumer, stream)`, so `stream_sequence` and the
`Nats-Msg-Id` of a dead letter, both read from `sequence.stream`, could never be produced from a
delivery the harness made.
"""

import dataclasses
import json

import nats.aio.msg as natsmsg
import pytest
from nats.errors import NotJSMessageError

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.dispatch.dlq import DeadLetterPublisher, message_metadata
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import MockJetStreamMetadata, MockMessage, ServiceTestHarness

pytestmark = pytest.mark.unit

REAL_REPLY = "$JS.ACK.DECLARED.prober.3.42.7.1700000000000000000.0"


def test_a_message_given_no_metadata_raises_as_a_core_message_does():
    with pytest.raises(NotJSMessageError):
        _ = MockMessage("orders.created").metadata

    with pytest.raises(NotJSMessageError):
        getattr(MockMessage("orders.created"), "metadata", None)  # the hazard the helper exists for


def test_the_message_still_answers_with_the_metadata_it_was_given():
    delivery = MockJetStreamMetadata(num_delivered=4)

    assert MockMessage("orders.created", metadata=delivery).metadata is delivery
    assigned = MockMessage("orders.created")
    assigned.metadata = delivery
    assert assigned.metadata is delivery


def test_the_mock_sequence_is_the_real_sequence_pair():
    real = natsmsg.Msg.Metadata._from_reply(REAL_REPLY)
    mock = MockJetStreamMetadata()

    assert type(mock.sequence) is type(real.sequence)
    assert isinstance(mock.sequence.stream, int) and isinstance(mock.sequence.consumer, int)


def test_the_mock_metadata_carries_every_field_the_dispatcher_and_the_dead_letter_read():
    names = {field.name for field in dataclasses.fields(MockJetStreamMetadata)}

    assert {"num_delivered", "stream", "consumer", "sequence"} <= names


def test_CONTROL_the_dead_letter_origin_of_a_message_with_no_metadata_names_no_stream():
    publisher = DeadLetterPublisher(ServiceConfig(name="svc", health_port=0), lambda: None)

    fields, headers = publisher.origin(MockMessage("orders.created"))

    assert not {"stream", "stream_sequence", "consumer"} & fields.keys()
    assert "Nats-Msg-Id" not in headers
    assert message_metadata(MockMessage("orders.created")) is None


class Failing(CliffracerService):
    @listener("probe.declared.thing", durable="prober")
    async def on_thing(self, subject: str, value: int = 0) -> None:
        raise RuntimeError("handler exploded")


async def test_a_dead_letter_from_the_harness_carries_the_stream_sequence_and_the_dedup_id():
    config = ServiceConfig(
        name="probe_svc",
        health_port=0,
        jetstream_enabled=True,
        jetstream_max_deliver=3,
        jetstream_streams=[
            StreamSpec(name="DECLARED", subjects=["probe.declared.>"]),
            StreamSpec(name="DLQ", subjects=["dlq.>"]),
        ],
    )

    async with ServiceTestHarness(Failing, config=config) as harness:
        await harness.deliver_jetstream("probe.declared.thing", {"value": 1}, num_delivered=3)
        _, payload, headers = harness.jetstream.published[-1]

    record = json.loads(payload)
    assert record["stream"] == "MOCK" and record["consumer"] == "mock-consumer"
    assert record["stream_sequence"] == 1
    assert headers["Nats-Msg-Id"] == "dlq:probe_svc:MOCK:1:mock-consumer"
