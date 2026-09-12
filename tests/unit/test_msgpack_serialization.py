"""Unit tests for binary serialization (MsgPack) support."""

import decimal
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import msgpack
import pytest
from pydantic import BaseModel, ValidationError

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RPCError
from cliffracer.core.validation import (
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_MSGPACK,
    deserialize_payload,
    pack_msgpack,
    serialize_payload,
    unpack_msgpack,
)

pytestmark = pytest.mark.unit


class SubItem(BaseModel):
    label: str
    amount: decimal.Decimal


class ComplexModel(BaseModel):
    item_id: uuid.UUID
    name: str
    created_at: datetime
    sub_items: list[SubItem]
    raw_data: bytes = b""


class EchoRequest(BaseModel):
    message: str
    count: int = 1


class EchoResponse(BaseModel):
    echoed: str
    count: int


class _TestService(CliffracerService):
    @rpc
    async def echo(self, request: EchoRequest) -> EchoResponse:
        return EchoResponse(echoed=request.message, count=request.count)

    @rpc
    async def echo_complex(self, model: ComplexModel) -> ComplexModel:
        return model

    @rpc
    async def failing_method(self, value: str) -> str:
        raise RuntimeError("boom")

    @rpc
    async def async_action(self, item: str) -> None:
        self.received_async = item

    @listener("test.event", fanout=True)
    async def on_event(self, status: str) -> None:
        self.received_event = {"status": status}


class _MockMsg:
    def __init__(
        self,
        subject: str,
        data: bytes,
        headers: dict[str, str] | None = None,
        reply: str = "_INBOX.test",
    ):
        self.subject = subject
        self.data = data
        self.headers = headers or {}
        self.reply = reply
        self.response_bytes: bytes | None = None
        self.response_headers: dict[str, str] | None = None

    async def respond(self, payload: bytes):
        self.response_bytes = payload
        self.response_headers = dict(self.headers)


# 1. ServiceConfig tests
def test_service_config_serialization_format():
    # Default is "json"
    cfg_default = ServiceConfig(name="svc")
    assert cfg_default.serialization_format == "json"

    # Accepts "msgpack"
    cfg_msgpack = ServiceConfig(name="svc", serialization_format="msgpack")
    assert cfg_msgpack.serialization_format == "msgpack"

    # Rejects invalid formats
    with pytest.raises(ValidationError):
        ServiceConfig(name="svc", serialization_format="xml")

    with pytest.raises(ValidationError):
        ServiceConfig(name="svc", serialization_format=123)

    # Extra forbid respected
    with pytest.raises(ValidationError):
        ServiceConfig(**{"name": "svc", "invalid_field": "unexpected"})


# 2. Primitives roundtrip
def test_pack_and_unpack_msgpack_primitives():
    primitives = {
        "int": 42,
        "negative": -100,
        "float": 3.14159,
        "str": "hello cliffracer",
        "bool": True,
        "none": None,
        "list": [1, "two", 3.0, False],
        "nested": {"a": [1, 2, 3], "b": {"c": "d"}},
        "bytes": b"\x00\x01\x02\x03",
    }
    packed = pack_msgpack(primitives)
    assert isinstance(packed, bytes)
    unpacked = unpack_msgpack(packed)
    assert unpacked == primitives


# 3. Complex types & Pydantic models roundtrip
def test_pack_and_unpack_complex_pydantic_model():
    model = ComplexModel(
        item_id=uuid.uuid4(),
        name="test_item",
        created_at=datetime.now(UTC),
        sub_items=[
            SubItem(label="first", amount=decimal.Decimal("12.34")),
            SubItem(label="second", amount=decimal.Decimal("56.78")),
        ],
        raw_data=b"binary_payload",
    )
    packed = pack_msgpack(model)
    unpacked = unpack_msgpack(packed)
    rebuilt = ComplexModel.model_validate(unpacked)
    assert rebuilt.item_id == model.item_id
    assert rebuilt.name == model.name
    assert rebuilt.created_at == model.created_at
    assert rebuilt.sub_items[0].amount == decimal.Decimal("12.34")
    assert rebuilt.sub_items[1].amount == decimal.Decimal("56.78")
    assert rebuilt.raw_data == b"binary_payload"


# 4. serialize_payload and deserialize_payload tests
def test_serialize_payload():
    data = {"hello": "world", "num": 1}

    # msgpack
    raw_mp, ct_mp = serialize_payload(data, format="msgpack")
    assert ct_mp == CONTENT_TYPE_MSGPACK
    assert unpack_msgpack(raw_mp) == data

    # case insensitive
    raw_mp2, ct_mp2 = serialize_payload(data, format="MSGPACK")
    assert ct_mp2 == CONTENT_TYPE_MSGPACK
    assert unpack_msgpack(raw_mp2) == data

    # json
    raw_js, ct_js = serialize_payload(data, format="json")
    assert ct_js == CONTENT_TYPE_JSON
    import json

    assert json.loads(raw_js.decode("utf-8")) == data

    # invalid format
    with pytest.raises(ValueError, match="Unsupported serialization format"):
        serialize_payload(data, format="protobuf")


def test_deserialize_payload_edge_cases():
    data = {"key": "value", "id": 99}
    raw_mp = pack_msgpack(data)
    raw_js = serialize_payload(data, format="json")[0]

    # Empty payload
    assert deserialize_payload(b"") == {}
    assert deserialize_payload(None) == {}

    # Explicit content-type headers (including parameters)
    assert deserialize_payload(raw_mp, content_type="application/msgpack") == data
    assert deserialize_payload(raw_mp, content_type="APPLICATION/MSGPACK; charset=binary") == data
    assert deserialize_payload(raw_js, content_type="application/json") == data
    assert deserialize_payload(raw_js, content_type="application/json; charset=utf-8") == data

    # Untyped fallback
    assert deserialize_payload(raw_mp, content_type=None, fallback_format="msgpack") == data
    assert deserialize_payload(raw_js, content_type=None, fallback_format="json") == data

    # Graceful fallback when content_type is None
    # untyped JSON parsed by service preferring msgpack
    assert deserialize_payload(raw_js, content_type=None, fallback_format="msgpack") == data
    # untyped msgpack parsed by service preferring json
    assert deserialize_payload(raw_mp, content_type=None, fallback_format="json") == data

    # Corrupted msgpack bytes with explicit header raises error
    corrupted = b"\xc1\x00\x99\xff\xee"
    with pytest.raises((msgpack.exceptions.FormatError, msgpack.exceptions.ExtraData, ValueError)):
        deserialize_payload(corrupted, content_type="application/msgpack")

    # Corrupted json bytes with explicit header raises error
    with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
        deserialize_payload(b"{not json", content_type="application/json")


# 5. Inbound RPC dispatch tests
@pytest.fixture
async def json_svc():
    service = _TestService(ServiceConfig(name="test_svc", serialization_format="json"))
    await service.container._setup_extensions()
    service._discover_handlers()
    return service


@pytest.fixture
async def msgpack_svc():
    service = _TestService(ServiceConfig(name="test_svc", serialization_format="msgpack"))
    await service.container._setup_extensions()
    service._discover_handlers()
    return service


async def test_inbound_rpc_msgpack_request_and_response(json_svc):
    """A service configured for JSON receives MsgPack and replies in MsgPack."""
    req_payload = {"request": {"message": "hello msgpack", "count": 2}}
    req_bytes = pack_msgpack(req_payload)

    msg = _MockMsg(
        subject="test_svc.rpc.echo",
        data=req_bytes,
        headers={"Content-Type": CONTENT_TYPE_MSGPACK},
    )

    await json_svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    assert msg.response_headers.get("Content-Type") == CONTENT_TYPE_MSGPACK
    resp = unpack_msgpack(msg.response_bytes)
    assert resp["success"] is True
    assert resp["result"] == {"echoed": "hello msgpack", "count": 2}


async def test_inbound_rpc_json_request_to_msgpack_service(msgpack_svc):
    """A service configured for MsgPack receives JSON and replies in JSON."""
    req_payload = {"request": {"message": "hello json", "count": 3}}
    req_bytes, _ = serialize_payload(req_payload, format="json")

    msg = _MockMsg(
        subject="test_svc.rpc.echo",
        data=req_bytes,
        headers={"Content-Type": CONTENT_TYPE_JSON},
    )

    await msgpack_svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    assert msg.response_headers.get("Content-Type") == CONTENT_TYPE_JSON
    resp = deserialize_payload(msg.response_bytes, content_type=CONTENT_TYPE_JSON)
    assert resp["success"] is True
    assert resp["result"] == {"echoed": "hello json", "count": 3}


async def test_inbound_rpc_untyped_json_request_to_msgpack_service(msgpack_svc):
    """A legacy client without Content-Type header sends JSON to a msgpack service."""
    req_payload = {"request": {"message": "untyped json", "count": 1}}
    req_bytes, _ = serialize_payload(req_payload, format="json")

    msg = _MockMsg(
        subject="test_svc.rpc.echo",
        data=req_bytes,
        headers={},  # no Content-Type header
    )

    await msgpack_svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    assert msg.response_headers.get("Content-Type") == CONTENT_TYPE_JSON
    resp = deserialize_payload(msg.response_bytes, content_type=CONTENT_TYPE_JSON)
    assert resp["success"] is True
    assert resp["result"] == {"echoed": "untyped json", "count": 1}


async def test_inbound_rpc_msgpack_validation_failure_envelope(json_svc):
    """Validation failure on MsgPack payload returns structured error envelope in MsgPack."""
    invalid_payload = {
        "request": {"message": "missing count is ok, but count is string", "count": "not_an_int"}
    }
    req_bytes = pack_msgpack(invalid_payload)

    msg = _MockMsg(
        subject="test_svc.rpc.echo",
        data=req_bytes,
        headers={"Content-Type": CONTENT_TYPE_MSGPACK},
    )

    await json_svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    assert msg.response_headers.get("Content-Type") == CONTENT_TYPE_MSGPACK
    resp = unpack_msgpack(msg.response_bytes)
    assert resp["success"] is False
    assert resp["error"] == "validation failed"
    assert "details" in resp


async def test_inbound_rpc_corrupted_msgpack_bytes(json_svc):
    """Corrupted binary bytes with MsgPack header returns validation error envelope."""
    corrupted_bytes = b"\xc1\xff\x00\xee\x12"

    msg = _MockMsg(
        subject="test_svc.rpc.echo",
        data=corrupted_bytes,
        headers={"Content-Type": CONTENT_TYPE_MSGPACK},
    )

    await json_svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    assert msg.response_headers.get("Content-Type") == CONTENT_TYPE_MSGPACK
    resp = unpack_msgpack(msg.response_bytes)
    assert resp["success"] is False
    assert resp["error"] == "validation failed"
    assert resp["details"][0]["loc"] == ["__root__"]


async def test_inbound_rpc_unknown_method_msgpack(json_svc):
    """Unknown method request with MsgPack Content-Type replies in MsgPack."""
    msg = _MockMsg(
        subject="test_svc.rpc.unknown_method",
        data=pack_msgpack({}),
        headers={"Content-Type": CONTENT_TYPE_MSGPACK},
    )

    await json_svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    assert msg.response_headers.get("Content-Type") == CONTENT_TYPE_MSGPACK
    resp = unpack_msgpack(msg.response_bytes)
    assert "error" in resp
    assert "Unknown method" in resp["error"]


async def test_inbound_rpc_handler_exception_msgpack(json_svc):
    """Handler exception with MsgPack Content-Type replies in MsgPack."""
    msg = _MockMsg(
        subject="test_svc.rpc.failing_method",
        data=pack_msgpack({"value": "boom"}),
        headers={"Content-Type": CONTENT_TYPE_MSGPACK},
    )

    await json_svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    assert msg.response_headers.get("Content-Type") == CONTENT_TYPE_MSGPACK
    resp = unpack_msgpack(msg.response_bytes)
    assert "error" in resp
    assert "Internal server error" in resp["error"]
    assert "traceback" not in resp

    # Opt-in with expose_internal_errors=True
    opt_svc = _TestService(ServiceConfig(name="test_opt", expose_internal_errors=True))
    opt_svc._discover_handlers()
    opt_msg = _MockMsg(
        subject="test_opt.rpc.failing_method",
        data=pack_msgpack({"value": "boom"}),
        headers={"Content-Type": CONTENT_TYPE_MSGPACK},
    )
    await opt_svc.container._handle_rpc_request(opt_msg)
    opt_resp = unpack_msgpack(opt_msg.response_bytes)
    assert "error" in opt_resp
    assert "boom" in opt_resp["error"]
    assert "traceback" in opt_resp


# 6. Outbound RPC & Event calls
async def test_outbound_call_rpc_msgpack(msgpack_svc):
    """Outbound call_rpc sends MsgPack and attaches Content-Type header."""
    mock_nc = MagicMock()
    reply_payload = {"success": True, "result": {"echoed": "back", "count": 1}}
    reply_bytes = pack_msgpack(reply_payload)

    mock_reply = MagicMock()
    mock_reply.data = reply_bytes
    mock_reply.headers = {"Content-Type": CONTENT_TYPE_MSGPACK}

    mock_nc.request = AsyncMock(return_value=mock_reply)
    msgpack_svc.nc = mock_nc

    result = await msgpack_svc.call_rpc(
        "other_svc", "echo", request={"message": "back", "count": 1}
    )

    assert result == {"echoed": "back", "count": 1}
    mock_nc.request.assert_called_once()
    call_args = mock_nc.request.call_args
    sent_bytes = call_args[0][1]
    sent_headers = call_args[1]["headers"]

    # Verify request payload is msgpack
    assert unpack_msgpack(sent_bytes)["request"] == {"message": "back", "count": 1}
    assert sent_headers["Content-Type"] == CONTENT_TYPE_MSGPACK


async def test_outbound_call_rpc_json_svc_decodes_msgpack_reply(json_svc):
    """A service configured for JSON handles a MsgPack reply seamlessly."""
    mock_nc = MagicMock()
    reply_payload = {"success": True, "result": "msgpack response"}
    reply_bytes = pack_msgpack(reply_payload)

    mock_reply = MagicMock()
    mock_reply.data = reply_bytes
    mock_reply.headers = {"Content-Type": CONTENT_TYPE_MSGPACK}

    mock_nc.request = AsyncMock(return_value=mock_reply)
    json_svc.nc = mock_nc

    result = await json_svc.call_rpc("other_svc", "plain", value="hi")
    assert result == "msgpack response"


async def test_outbound_call_rpc_raises_rpc_error(msgpack_svc):
    mock_nc = MagicMock()
    mock_reply = MagicMock()
    mock_reply.data = pack_msgpack({"error": "remote failure", "details": [{"msg": "err"}]})
    mock_reply.headers = {"Content-Type": CONTENT_TYPE_MSGPACK}
    mock_nc.request = AsyncMock(return_value=mock_reply)
    msgpack_svc.nc = mock_nc

    with pytest.raises(RPCError) as exc_info:
        await msgpack_svc.call_rpc("other_svc", "failing", x=1)
    assert "remote failure" in str(exc_info.value)
    assert exc_info.value.details == [{"msg": "err"}]


async def test_outbound_call_async_and_no_wait_msgpack(msgpack_svc):
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    msgpack_svc.nc = mock_nc

    await msgpack_svc.call_async("other_svc", "notify", event="start")
    mock_nc.publish.assert_called_once()
    _, sent_bytes = mock_nc.publish.call_args[0][:2]
    headers = mock_nc.publish.call_args[1]["headers"]
    assert unpack_msgpack(sent_bytes)["event"] == "start"
    assert headers["Content-Type"] == CONTENT_TYPE_MSGPACK

    mock_nc.publish.reset_mock()
    await msgpack_svc.call_rpc_no_wait("other_svc", "fire", param=42)
    mock_nc.publish.assert_called_once()
    _, sent_bytes = mock_nc.publish.call_args[0][:2]
    headers = mock_nc.publish.call_args[1]["headers"]
    assert unpack_msgpack(sent_bytes)["param"] == 42
    assert headers["Content-Type"] == CONTENT_TYPE_MSGPACK


async def test_outbound_publish_event_msgpack(msgpack_svc):
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    msgpack_svc.nc = mock_nc

    await msgpack_svc.publish_event("orders.created", order_id="12345")
    mock_nc.publish.assert_called_once()
    _, sent_bytes = mock_nc.publish.call_args[0][:2]
    headers = mock_nc.publish.call_args[1]["headers"]
    unpacked = unpack_msgpack(sent_bytes)
    assert unpacked["order_id"] == "12345"
    assert headers["Content-Type"] == CONTENT_TYPE_MSGPACK


# 7. Inbound async and event handlers with MsgPack
async def test_inbound_async_request_msgpack(msgpack_svc):
    msg = _MockMsg(
        subject="test_svc.async.async_action",
        data=pack_msgpack({"item": "apple"}),
        headers={"Content-Type": CONTENT_TYPE_MSGPACK},
    )

    await msgpack_svc.container._handle_async_request(msg)
    assert msgpack_svc.received_async == "apple"


async def test_inbound_event_dispatch_msgpack(msgpack_svc):
    msg = _MockMsg(
        subject="test.event",
        data=pack_msgpack({"status": "active"}),
        headers={"Content-Type": CONTENT_TYPE_MSGPACK},
    )

    outcome = await msgpack_svc.container._dispatch_event(msg)
    assert outcome.value == "ok"
    assert msgpack_svc.received_event == {"status": "active"}


# 8. CliffracerClient content negotiation
async def test_client_content_type_negotiation():
    client = ServiceClient(service="test_svc", nats_url="nats://localhost:4222", verify=False)
    client._verified = True

    # Mock _connection and _request
    client._connection = AsyncMock()

    mock_reply = MagicMock()
    mock_reply.data = pack_msgpack({"success": True, "result": {"echoed": "hi", "count": 1}})
    mock_reply.headers = {"Content-Type": CONTENT_TYPE_MSGPACK}

    client._request = AsyncMock(return_value=mock_reply)

    res = await client._call("echo", {"message": "hi", "count": 1}, EchoResponse)
    assert isinstance(res, EchoResponse)
    assert res.echoed == "hi"
    assert res.count == 1
