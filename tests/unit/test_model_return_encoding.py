"""Tests ensuring handler Pydantic model returns are properly encoded."""

import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing import refuse_a_reply_with_no_subject

pytestmark = pytest.mark.unit


class Out(BaseModel):
    ok: bool
    name: str


class In(BaseModel):
    name: str


class _Svc(CliffracerService):
    @rpc
    async def plain(self) -> Out:
        return Out(ok=True, name="plain")

    @rpc
    async def takes_a_model(self, request: In) -> Out:
        return Out(ok=True, name=request.name)


class _Msg:
    #: Every dispatcher path reads this; a double without one let a
    #: reply be recorded that production would have refused.
    reply: str | None = "_INBOX.test"

    def __init__(self, subject: str, payload: dict):
        self.subject = subject
        self.data = json.dumps(payload).encode()
        self.headers: dict[str, str] = {}
        self.response: dict | None = None

    async def respond(self, payload: bytes) -> None:
        refuse_a_reply_with_no_subject(self)
        self.response = json.loads(payload.decode())


async def _dispatch(subject: str, payload: dict) -> dict:
    svc = _Svc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _Msg(subject, payload)
    await svc.container._handle_rpc_request(msg)
    return msg.response


async def test_a_handlers_model_return_is_encoded():
    """Verify handler returning Pydantic model produces serialized dict in response."""
    response = await _dispatch("s.rpc.plain", {})

    assert "error" not in response, response
    assert response["result"] == {"ok": True, "name": "plain"}


async def test_a_model_parameter_does_not_change_the_return_encoding():
    """Verify parameter model input does not interfere with return encoding."""
    response = await _dispatch("s.rpc.takes_a_model", {"request": {"name": "alice"}})

    assert response["success"] is True
    assert response["result"] == {"ok": True, "name": "alice"}
