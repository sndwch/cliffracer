"""Tests verifying uniform RPC reply envelope schema across all handlers."""

import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit


class CreateUser(BaseModel):
    username: str
    email: str


class User(BaseModel):
    user_id: str


class _Svc(CliffracerService):
    @rpc
    async def create_user(self, request: CreateUser) -> User:
        return User(user_id=f"user_{request.username}")

    @rpc
    async def plain(self, value: str) -> str:
        return value

    @rpc
    async def with_cid(self, value: str, correlation_id: str | None = None) -> str:
        return f"{value}:{correlation_id}"


class _MockMsg:
    def __init__(self, subject, data: dict):
        self.subject = subject
        self.data = json.dumps(data).encode()
        self.headers: dict[str, str] = {}
        self.response = None

    async def respond(self, payload: bytes):
        self.response = json.loads(payload.decode())


@pytest.fixture
async def svc():
    service = _Svc(ServiceConfig(name="envelope_svc"))
    await service.container._setup_extensions()
    service._discover_handlers()
    return service


async def test_a_model_parameter_is_validated_and_a_model_return_is_encoded(svc):
    msg = _MockMsg(
        "envelope_svc.rpc.create_user", {"request": {"username": "alice", "email": "a@b"}}
    )
    await svc.container._handle_rpc_request(msg)
    assert msg.response["success"] is True
    assert msg.response["result"] == {"user_id": "user_alice"}


async def test_validation_failure_envelope(svc):
    msg = _MockMsg("envelope_svc.rpc.create_user", {"request": {"username": "alice"}})
    await svc.container._handle_rpc_request(msg)
    assert msg.response["success"] is False
    assert msg.response["error"] == "validation failed"
    assert any(e.get("loc") == ["request", "email"] for e in msg.response["details"])
    assert "traceback" not in msg.response


async def test_an_extra_key_is_a_validation_failure(svc):
    msg = _MockMsg("envelope_svc.rpc.plain", {"value": "hello", "bogus": 1})
    await svc.container._handle_rpc_request(msg)
    assert msg.response["success"] is False
    assert any("bogus" in str(e.get("loc")) for e in msg.response["details"])


async def test_correlation_id_in_the_body_is_not_an_extra_key(svc):
    """Every caller puts correlation_id in the body today (call_rpc). It must keep working."""
    msg = _MockMsg("envelope_svc.rpc.plain", {"value": "hello", "correlation_id": "abc"})
    await svc.container._handle_rpc_request(msg)
    assert msg.response["success"] is True and msg.response["result"] == "hello"


async def test_correlation_id_is_injected_only_when_declared(svc):
    msg = _MockMsg("envelope_svc.rpc.with_cid", {"value": "v", "correlation_id": "cid-1"})
    await svc.container._handle_rpc_request(msg)
    assert msg.response["result"].startswith("v:")


async def test_every_success_reply_carries_success_true(svc):
    """One envelope. The old plain shape (no `success` key) is gone."""
    msg = _MockMsg("envelope_svc.rpc.plain", {"value": "hello"})
    await svc.container._handle_rpc_request(msg)
    assert msg.response["success"] is True
    assert msg.response["result"] == "hello"
    assert "correlation_id" in msg.response


async def test_a_flat_payload_for_a_model_parameter_fails_loudly(svc):
    """The upgrade hazard, made audible.

    A legacy caller sent a validated handler the model's fields FLAT and the old
    registry unpacked them into the schema. The parameter is the schema now, so
    the payload nests under the parameter name and the same flat call is
    wrong. It has to answer a validation failure naming `request`, not silently
    do something else: the failure a caller can read is the whole reason the
    break is safe to ship.
    """
    msg = _MockMsg("envelope_svc.rpc.create_user", {"username": "alice", "email": "a@b"})
    await svc.container._handle_rpc_request(msg)

    assert msg.response["success"] is False
    assert msg.response["error"] == "validation failed"
    locs = [e.get("loc") for e in msg.response["details"]]
    assert ["request"] in locs, locs
    types = {e.get("type") for e in msg.response["details"]}
    assert "missing" in types and "extra_forbidden" in types, msg.response["details"]


async def test_the_handler_never_runs_on_invalid_input(svc):
    ran = []
    original = svc.container.registry.rpc_handlers["create_user"]

    async def spy(request):
        ran.append(request)
        return await original(request)

    svc.container.registry.rpc_handlers["create_user"] = spy
    await svc.container._handle_rpc_request(
        _MockMsg("envelope_svc.rpc.create_user", {"request": {"username": "x"}})
    )
    assert ran == []


async def test_a_wrong_typed_return_is_a_server_error_not_a_lie(svc):
    """A handler returning something its annotation does not describe answers an error."""

    async def bad(value: str) -> str:
        return 42  # type: ignore[return-value]

    svc.container.registry.rpc_handlers["plain"] = bad
    msg = _MockMsg("envelope_svc.rpc.plain", {"value": "hello"})
    await svc.container._handle_rpc_request(msg)
    assert msg.response.get("success") is not True
    assert "error" in msg.response


@pytest.mark.parametrize("bad_payload", [42, "hello", [1, 2], None])
async def test_non_dict_payload_returns_validation_error_envelope(svc, bad_payload):
    msg = _MockMsg("envelope_svc.rpc.plain", bad_payload)
    await svc.container._handle_rpc_request(msg)
    assert msg.response["success"] is False
    assert msg.response["error"] == "validation failed"
    assert "details" in msg.response
    assert "traceback" not in msg.response


async def test_non_json_bytes_returns_validation_error_envelope_and_responds(svc):
    """Verify raw unparseable or non-UTF8 bytes respond with validation error envelope."""
    msg = _MockMsg("envelope_svc.rpc.plain", None)
    msg.data = b"NOT_JSON_DATA{{{"
    await svc.container._handle_rpc_request(msg)
    assert "details" in msg.response


async def test_bare_exception_fallback_returns_class_name(svc):
    """Verify that bare exceptions without message fallback to exception class name."""

    async def raise_bare(*args, **kwargs):
        raise RuntimeError()

    svc.container.registry.rpc_handlers["plain"] = raise_bare
    msg = _MockMsg("envelope_svc.rpc.plain", {"value": "hello"})
    await svc.container._handle_rpc_request(msg)
    assert msg.response["success"] is False
    assert "Internal server error" in msg.response["error"]
    assert "traceback" not in msg.response

    # Opt-in with expose_internal_errors=True
    opt_svc = _Svc(ServiceConfig(name="envelope_opt", expose_internal_errors=True))
    opt_svc._discover_handlers()
    opt_svc.container.registry.rpc_handlers["plain"] = raise_bare
    opt_msg = _MockMsg("envelope_opt.rpc.plain", {"value": "hello"})
    await opt_svc.container._handle_rpc_request(opt_msg)
    assert opt_msg.response["success"] is False
    assert opt_msg.response["error"] == "RuntimeError"
    assert "traceback" in opt_msg.response


async def test_bare_exception_fallback_in_describe(svc, monkeypatch):
    """Verify describe error handler falls back to class name when exception has no message."""
    import cliffracer.introspect

    def broken_canonical(*args, **kwargs):
        raise ValueError()

    monkeypatch.setattr(cliffracer.introspect, "canonical", broken_canonical)
    msg = _MockMsg("envelope_svc.describe", {})
    await svc.container._handle_describe_request(msg)
    assert msg.response["success"] is False
    assert msg.response["error"] == "ValueError"
