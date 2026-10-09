"""A real service with a strict model, on a real broker, takes calls in the forms a client sends.

A client sends the JSON form of a model's dump, over JSON and over msgpack (cliffracer's own senders
dump to JSON values before they pack), and a service with a strict model used to refuse both. A
foreign msgpack producer sends python values, and those are read as they were.
"""

import msgpack
import pytest

from cliffracer import ServiceConfig
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.validation import (
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_MSGPACK,
    deserialize_payload,
    pack_msgpack,
    serialize_payload,
)
from tests.fixtures.strict_payloads import DUMP, RECORD, Records

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


@pytest.fixture
async def service():
    Records.received = []
    records = Records(ServiceConfig(name="strict_records_e2e", health_listener=False))
    await records.start()
    try:
        yield records
    finally:
        await records.stop()


async def _call(service, nats_connection, method: str, body: bytes, content_type: str) -> dict:
    subject = HandlerDiscovery.outbound_subject(service.config, service.config.name, "rpc", method)
    reply = await nats_connection.request(
        subject, body, timeout=5, headers={"Content-Type": content_type}
    )
    return deserialize_payload(
        reply.data, content_type=(reply.headers or {}).get("Content-Type", CONTENT_TYPE_JSON)
    )


async def test_json_and_msgpack_calls_with_the_json_form_of_a_strict_models_dump_are_accepted(
    service, nats_connection
):
    as_json = await _call(
        service, nats_connection, "put", *serialize_payload({"record": DUMP}, format="json")
    )
    as_msgpack = await _call(
        service, nats_connection, "put", pack_msgpack({"record": DUMP}), CONTENT_TYPE_MSGPACK
    )

    assert as_json["success"] is True and as_msgpack["success"] is True, (as_json, as_msgpack)
    assert service.received == [RECORD, RECORD]


async def test_a_foreign_msgpack_producers_bytes_are_still_accepted(service, nats_connection):
    body = msgpack.packb({"blob": {"raw": b"\xff\x00"}}, use_bin_type=True)

    reply = await _call(service, nats_connection, "blob", body, CONTENT_TYPE_MSGPACK)

    assert reply["success"] is True and reply["result"] == 2, reply


async def test_a_strict_model_still_refuses_text_for_an_int_over_the_broker(
    service, nats_connection
):
    body, content_type = serialize_payload({"record": {**DUMP, "count": "2"}}, format="json")

    reply = await _call(service, nats_connection, "put", body, content_type)

    assert reply["success"] is False and service.received == [], reply
