"""Tests verifying uniform RPC reply envelope schema across all handlers."""

import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing import refuse_a_reply_with_no_subject

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
    #: Every dispatcher path reads this; a double without one let a
    #: reply be recorded that production would have refused.
    reply: str | None = "_INBOX.test"

    def __init__(self, subject, data: dict):
        self.subject = subject
        self.data = json.dumps(data).encode()
        self.headers: dict[str, str] = {}
        self.response = None

    async def respond(self, payload: bytes):
        refuse_a_reply_with_no_subject(self)
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


async def test_a_declared_correlation_id_parameter_receives_the_requests_id(svc):
    msg = _MockMsg("envelope_svc.rpc.with_cid", {"value": "v", "correlation_id": "cid-1"})
    await svc.container._handle_rpc_request(msg)
    assert msg.response["result"] == "v:cid-1"


async def test_the_correlation_id_is_not_injected_into_a_handler_that_does_not_declare_it(svc):
    """`plain` declares only `value`: an injected `correlation_id` keyword would be a TypeError,
    and the reply would be a failure instead of "hello"."""
    msg = _MockMsg("envelope_svc.rpc.plain", {"value": "hello", "correlation_id": "cid-2"})
    await svc.container._handle_rpc_request(msg)
    assert msg.response["success"] is True
    assert msg.response["result"] == "hello"
    assert msg.response["correlation_id"] == "cid-2"


async def test_every_success_reply_carries_success_true(svc):
    """One envelope. The old plain shape (no `success` key) is gone."""
    msg = _MockMsg("envelope_svc.rpc.plain", {"value": "hello", "correlation_id": "cid-echo"})
    await svc.container._handle_rpc_request(msg)
    assert msg.response["success"] is True
    assert msg.response["result"] == "hello"
    # The request's own id, not merely a key: a null or fresh id in its place would still be present.
    assert msg.response["correlation_id"] == "cid-echo"


async def test_a_success_reply_to_a_request_with_no_id_carries_a_generated_one(svc):
    msg = _MockMsg("envelope_svc.rpc.plain", {"value": "hello"})
    await svc.container._handle_rpc_request(msg)
    assert msg.response["success"] is True
    assert isinstance(msg.response["correlation_id"], str) and msg.response["correlation_id"]


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
    msg = _MockMsg("envelope_svc.rpc.create_user", {"request": {"username": "x"}})
    await svc.container._handle_rpc_request(msg)
    # `ran == []` alone is also true when the dispatcher returns before doing
    # anything, so the caller must be shown to have been refused, and why.
    assert msg.response is not None, "the caller was left without a reply"
    assert msg.response["success"] is False
    assert msg.response["error"] == "validation failed"
    assert [(d["type"], d["loc"]) for d in msg.response["details"]] == [
        ("missing", ["request", "email"])
    ]
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


@pytest.mark.parametrize("raw", [b"NOT_JSON_DATA{{{", b"\x80\xff"], ids=["not-json", "not-utf8"])
async def test_non_json_bytes_returns_validation_error_envelope_and_responds(svc, raw):
    """Raw unparseable or non-UTF8 bytes answer a payload_invalid validation envelope."""
    msg = _MockMsg("envelope_svc.rpc.plain", None)
    msg.data = raw
    await svc.container._handle_rpc_request(msg)
    assert msg.response is not None, "the caller was left without a reply"
    assert msg.response["success"] is False
    assert msg.response["error"] == "validation failed"
    assert [d["type"] for d in msg.response["details"]] == ["payload_invalid"]
    assert "traceback" not in msg.response


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


async def test_bare_exception_fallback_in_describe(monkeypatch):
    """Verify describe error handler falls back to class name when exception has no message.

    The fallback is a property of the EXPOSED form, so the service is built
    with `expose_internal_errors=True` rather than taking the `svc` fixture's
    default. Read against the default this assertion was pinning the absence of
    a gate on the describe path: the class name reached the caller whatever the
    flag said. The withheld half is the sibling below.
    """
    import cliffracer.introspect

    def broken_canonical(*args, **kwargs):
        raise ValueError()

    monkeypatch.setattr(cliffracer.introspect, "canonical", broken_canonical)
    svc = _Svc(ServiceConfig(name="envelope_svc", expose_internal_errors=True))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _MockMsg("envelope_svc.describe", {})
    await svc.container._handle_describe_request(msg)
    assert msg.response["success"] is False
    assert msg.response["error"] == "ValueError"


async def test_a_bare_exception_in_describe_is_withheld_by_default(svc, monkeypatch):
    """CONTROL for the pair above: unset, not even the class name leaves."""
    import cliffracer.introspect

    def broken_canonical(*args, **kwargs):
        raise ValueError()

    monkeypatch.setattr(cliffracer.introspect, "canonical", broken_canonical)
    msg = _MockMsg("envelope_svc.describe", {})
    await svc.container._handle_describe_request(msg)
    assert msg.response["success"] is False
    assert msg.response["error"].startswith("Internal server error")
    assert "ValueError" not in msg.response["error"]
