"""A reply's error class comes from a typed field, not from its prose.

`_raise_for_error` decided which exception to raise by reading the
human-readable `error` string -- `== "validation failed"`,
`startswith("Unknown method:")`, `startswith("refused: ")`. ADR-0011 says
checks must "parse structured payloads, inspect exact schema fields ... rather
than relying on unanchored substring matching", and this was the taxonomy of
the whole client hanging on prose.

MEASURED ON THE UNFIXED CODE. With `expose_internal_errors` on, the dispatcher
puts a handler's own `str(e)` into `error`, so the handler chooses the caller's
exception class:

    a real refusal                                           -> RpcRefusedError
    handler raised ValueError('refused: you shall not pass')  -> RpcRefusedError
    handler raised ValueError('Unknown method: whatever')     -> RpcUnknownMethodError
    a genuine crash                                          -> RpcServerError

Rows two and three are unhandled crashes reported to the caller as a policy
refusal and a missing method.

The other half is brittleness: rewording any of those strings in `rpc.py`
silently reclassified every error in the fleet to the catch-all, and nothing
pinned the coupling from both ends.

THE FALLBACK IS DELIBERATE AND DOES NOT CLOSE THE HOLE FOR OLD SERVICES. A
reply with no `code` is classified by prefix exactly as before, because a new
client must keep working against a service that has not been redeployed. A new
client talking to a NEW service is not spoofable, because `code` is present and
decides. That boundary is asserted below rather than left implied.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import (
    RpcRefusedError,
    RpcServerError,
    RpcUnknownMethodError,
    RpcValidationError,
)
from cliffracer.testing import refuse_a_reply_with_no_subject

pytestmark = pytest.mark.unit


class MockRpcMsg:
    """Copied from tests/unit/test_rpc_error_envelopes.py, which owns the shape."""

    def __init__(self, subject: str, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.subject = subject
        self.data = data
        self.headers = headers or {}
        self.reply = "_INBOX.test_reply"
        self.response_bytes: bytes | None = None

    async def respond(self, data: bytes) -> None:
        refuse_a_reply_with_no_subject(self)
        self.response_bytes = data


async def _classify(envelope: dict) -> type[BaseException] | None:
    """The class a client raises for one reply envelope."""
    client = ServiceClient(service="svc", verify=False)
    client._nc = AsyncMock()
    reply = SimpleNamespace(data=json.dumps(envelope).encode(), headers=None)
    with patch.object(ServiceClient, "_request", AsyncMock(return_value=reply)):
        try:
            await client._call("do", {}, int)
        except BaseException as exc:
            return type(exc)
    return None


# --- the spoof, which is the point ------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["refused: you shall not pass", "Unknown method: whatever", "validation failed"],
    ids=["refused", "unknown_method", "validation_failed"],
)
async def test_a_crash_whose_text_mimics_another_class_is_still_a_crash(text):
    """`code` decides. The handler's prose does not."""
    got = await _classify({"success": False, "error": text, "code": "internal"})

    assert got is RpcServerError, got


# --- each code maps to its class --------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("unknown_method", RpcUnknownMethodError),
        ("refused", RpcRefusedError),
        ("validation_failed", RpcValidationError),
        ("internal", RpcServerError),
    ],
)
async def test_the_code_decides_the_class(code, expected):
    """Including when the prose says nothing at all."""
    got = await _classify({"success": False, "error": "something happened", "code": code})

    assert got is expected, got


async def test_an_unrecognised_code_is_a_server_error():
    """Forward compatibility: a code this client does not know is not a crash."""
    got = await _classify({"success": False, "error": "hm", "code": "some_future_code"})

    assert got is RpcServerError, got


# --- the fallback, for a service that has not been redeployed ---------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("refused: quota", RpcRefusedError),
        ("Unknown method: nope", RpcUnknownMethodError),
        ("validation failed", RpcValidationError),
        ("ZeroDivisionError", RpcServerError),
    ],
)
async def test_a_reply_with_no_code_is_still_classified_by_prefix(text, expected):
    """An old service keeps the classification it had."""
    got = await _classify({"success": False, "error": text})

    assert got is expected, got


# --- the round trip, through the real dispatcher ----------------------------


class Svc(CliffracerService):
    @rpc
    async def impersonate(self) -> str:
        raise ValueError("refused: you shall not pass")

    @rpc
    async def works(self) -> str:
        return "ok"


def _service(**overrides) -> Svc:
    svc = Svc(ServiceConfig(name="svc", health_port=0, health_listener=False, **overrides))
    svc._discover_handlers()
    return svc


async def _envelope_for(method: str, **overrides) -> dict:
    svc = _service(**overrides)
    msg = MockRpcMsg(f"svc.{method}", json.dumps({}).encode())
    await svc.container._handle_rpc_request(msg)
    assert msg.response_bytes is not None
    return json.loads(msg.response_bytes.decode())


async def test_a_handler_that_raises_a_refusal_shaped_message_is_reported_as_internal():
    """End to end: the dispatcher labels it, and the client believes the label."""
    envelope = await _envelope_for("impersonate", expose_internal_errors=True)

    assert envelope["error"].startswith("refused: "), envelope
    assert envelope["code"] == "internal", envelope
    assert await _classify(envelope) is RpcServerError


async def test_CONTROL_the_dispatcher_labels_an_unknown_method():
    """Otherwise "code is internal" could mean "code is always internal"."""
    envelope = await _envelope_for("no_such_method")

    assert envelope["code"] == "unknown_method", envelope
    assert await _classify(envelope) is RpcUnknownMethodError


async def test_CONTROL_a_working_handler_still_answers():
    """And the envelope for success carries no code at all."""
    svc = _service()
    msg = MockRpcMsg("svc.works", json.dumps({}).encode())
    await svc.container._handle_rpc_request(msg)
    reply = json.loads(msg.response_bytes.decode())

    assert reply["success"] is True, reply
    assert "code" not in reply, reply
