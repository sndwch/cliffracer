"""A dead-letter record carries its cause in a field, and the inspector reads that field.

`cliffracer-dlq` told the three kinds of record apart by shape, and told a decode failure from a
handler that ran out of deliveries by the text of `error`: a record whose `error` started with
"Decode error:" was a decode failure. A handler that raised `ValueError("Decode error: ...")` on
its last delivery was then counted as one, and `ls --cause decode` listed it. ADR-0011 permits an
exception class, a typed code or a field as the signal and never message text, so each record now
carries `cause`, set where the record is built. A record published before the field existed has
none, and is classified by shape as before.
"""

import json

import pytest
from cliffracer_dlq import records
from cliffracer_dlq.records import CAUSES, classify
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import MockMessage, ServiceTestHarness
from cliffracer.testing.messages import MockJetStreamMetadata

pytestmark = pytest.mark.unit

SUBJECT = "probe.declared.thing"


class Strict(BaseModel):
    value: int


class Consumer(CliffracerService):
    @listener(SUBJECT, durable="prober")
    async def on_thing(self, strict: Strict) -> None:
        if strict.value == 99:
            raise ValueError("Decode error: the handler's own text, not the framework's")
        if strict.value == 98:
            raise ValueError("db down")


def _config(*, expose: bool = False) -> ServiceConfig:
    return ServiceConfig(
        name="consumer_svc",
        health_port=0,
        expose_internal_errors=expose,
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="DECLARED", subjects=["probe.declared.>"]),
            StreamSpec(name="DLQ", subjects=["dlq.>"]),
        ],
    )


async def _records_after(body: bytes, *, num_delivered: int, expose: bool = False) -> list[dict]:
    async with ServiceTestHarness(Consumer, config=_config(expose=expose)) as harness:
        msg = MockMessage(
            subject=SUBJECT,
            data=body,
            headers={"Content-Type": "application/json"},
            metadata=MockJetStreamMetadata(num_delivered=num_delivered),
        )
        await harness.container.dispatcher.jetstream.handle_jetstream_event(msg, pattern=SUBJECT)
        return [
            json.loads(payload)
            for subject, payload, _ in harness.jetstream.published
            if subject.startswith("dlq.")
        ]


async def test_a_body_that_cannot_be_decoded_is_a_decode_record():
    (record,) = await _records_after(b"{not json", num_delivered=1)

    assert record["cause"] == "decode"
    assert classify(record) == "decode"


async def test_a_payload_that_fails_its_schema_is_an_invalid_record():
    (record,) = await _records_after(json.dumps({"value": "not an int"}).encode(), num_delivered=1)

    assert record["cause"] == "invalid"
    assert classify(record) == "invalid"


async def test_a_handler_that_runs_out_of_deliveries_is_a_delivery_limit_record():
    (record,) = await _records_after(json.dumps({"value": 98}).encode(), num_delivered=5)

    assert record["cause"] == "delivery-limit"
    assert classify(record) == "delivery-limit"


async def test_a_handler_error_that_reads_like_the_decode_prefix_is_still_a_delivery_limit_record():
    # With the text allowed out, so the record holds the handler's own words, which read like a
    # decode failure; the field is what decides.
    (record,) = await _records_after(
        json.dumps({"value": 99}).encode(), num_delivered=5, expose=True
    )

    assert record["error"].startswith("Decode error:")
    assert record["cause"] == "delivery-limit"
    assert classify(record) == "delivery-limit"


def test_the_field_decides_over_the_text_of_the_error():
    record = {
        "deliveries": 5,
        "error": "Decode error: it only reads like one",
        "cause": "delivery-limit",
    }

    assert classify(record) == "delivery-limit"


@pytest.mark.parametrize("cause", CAUSES)
def test_every_cause_the_inspector_lists_is_one_the_publisher_writes(cause):
    from cliffracer.core.dispatch import dlq

    assert cause in {dlq.CAUSE_DECODE, dlq.CAUSE_DELIVERY_LIMIT, dlq.CAUSE_INVALID}
    assert classify({"cause": cause, "deliveries": 1, "error": "x", "errors": []}) == cause


def test_the_inspector_lists_exactly_the_causes_the_publisher_writes():
    """Iterating `CAUSES` cannot see a cause missing from it, so the publisher's are named here."""
    from cliffracer.core.dispatch import dlq

    assert set(CAUSES) == {dlq.CAUSE_DECODE, dlq.CAUSE_DELIVERY_LIMIT, dlq.CAUSE_INVALID}


@pytest.mark.parametrize("cause", ["decode", "delivery-limit", "invalid"])
def test_every_cause_the_publisher_writes_is_read_from_the_field_over_the_shape(cause):
    """The shape alone says delivery-limit here: no decode prefix and no `errors` list."""
    from cliffracer.core.dispatch import dlq

    assert cause in {dlq.CAUSE_DECODE, dlq.CAUSE_DELIVERY_LIMIT, dlq.CAUSE_INVALID}
    assert classify({"cause": cause, "deliveries": 1, "error": "boom"}) == cause


def test_CONTROL_a_record_written_before_the_field_existed_is_classified_by_its_shape():
    assert classify({"deliveries": 1, "error": "Decode error: bad"}) == records.DECODE
    assert classify({"deliveries": 5, "error": "boom"}) == records.DELIVERY_LIMIT
    assert classify({"errors": [{"loc": ["value"]}], "schema": "Strict"}) == records.INVALID
    assert classify({"service": "orders"}) is None


@pytest.mark.parametrize("cause", ["", "other", None, 3, ["decode"]])
def test_CONTROL_a_cause_the_inspector_does_not_know_is_ignored_and_the_shape_decides(cause):
    assert classify({"deliveries": 5, "error": "boom", "cause": cause}) == records.DELIVERY_LIMIT


def test_the_inspectors_spellings_of_the_causes_are_the_ones_the_service_writes():
    """The package spells them itself so it reads records from any framework version; this ties them."""
    from cliffracer.core.dispatch import dlq

    assert (records.DECODE, records.DELIVERY_LIMIT, records.INVALID) == (
        dlq.CAUSE_DECODE,
        dlq.CAUSE_DELIVERY_LIMIT,
        dlq.CAUSE_INVALID,
    )
