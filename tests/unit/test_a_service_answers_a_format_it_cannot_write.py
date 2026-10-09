"""A caller gets an answer even when the service cannot speak its encoding.

`reply_format` is taken from the REQUEST's content type, so a service that
never configured msgpack still tries to answer a msgpack request in msgpack.
Without the optional extra that raises inside `serialize_payload`, and the
failure was swallowed at DEBUG -- so `msg.respond` was never called, the caller
blocked until its own timeout, and one debug line was the only record.

This is reachable in any partial rollout, which is what an optional extra
creates: one side installs `cliffracer[msgpack]` and the other does not.

BOTH SIDES ARE ASSERTED HERE. The service must send something, and the client
must turn it into an exception from the documented family -- a reply the client
cannot classify is not much better than no reply.
"""

from __future__ import annotations

import json

import msgpack
import pytest
from pydantic import BaseModel

import cliffracer.core.validation as validation
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcClientError, RpcError, RpcServerError
from cliffracer.testing import refuse_a_reply_with_no_subject

pytestmark = pytest.mark.unit

MSGPACK_CT = "application/msgpack"
JSON_CT = "application/json"


class Pong(BaseModel):
    echo: str


class Svc(CliffracerService):
    @rpc
    async def ping(self, value: str) -> Pong:
        return Pong(echo=value)


class FakeMsg:
    """One inbound request, capturing whatever the dispatcher sends back."""

    def __init__(self, data: bytes, content_type: str) -> None:
        self.subject = "svc.ping"
        self.reply = "_INBOX.test"
        self.data = data
        self.headers = {"Content-Type": content_type}
        self.response: bytes | None = None

    async def respond(self, payload: bytes) -> None:
        refuse_a_reply_with_no_subject(self)
        self.response = payload


async def _drive(content_type: str, body: bytes, *, msgpack_installed: bool) -> FakeMsg:
    service = Svc(ServiceConfig(name="svc", health_listener=False))
    service.container.discover_handlers()
    saved = validation.msgpack
    if not msgpack_installed:
        validation.msgpack = None
    try:
        message = FakeMsg(body, content_type)
        await service.container.dispatcher.handle_rpc_request(message)
        return message
    finally:
        validation.msgpack = saved


# --- the service side -------------------------------------------------------


async def test_a_msgpack_request_is_answered_even_without_the_extra():
    """The defect: `respond` was never called and the caller waited for nothing."""
    message = await _drive(MSGPACK_CT, msgpack.packb({"value": "hi"}), msgpack_installed=False)

    assert message.response is not None, "the caller received no reply at all"


async def test_the_answer_names_the_missing_extra():
    """A caller has to be able to act on it, and the operator has to know what to install."""
    message = await _drive(MSGPACK_CT, msgpack.packb({"value": "hi"}), msgpack_installed=False)
    envelope = json.loads(message.response)

    assert envelope["success"] is False
    assert "cliffracer[msgpack]" in envelope["error"], envelope


async def test_the_answer_is_not_reported_as_the_callers_fault():
    """It used to arrive as `validation failed`, which sends a caller to its payload.

    The payload was fine. The service could not read the encoding, which is the
    service's own missing dependency.
    """
    message = await _drive(MSGPACK_CT, msgpack.packb({"value": "hi"}), msgpack_installed=False)
    envelope = json.loads(message.response)

    assert envelope["error"] != "validation failed"
    assert "details" not in envelope, "a validation envelope would carry field errors"


async def test_the_reply_declares_the_format_it_was_actually_written_in():
    """The client decodes by content type, so a JSON body labelled msgpack is unreadable."""
    message = await _drive(MSGPACK_CT, msgpack.packb({"value": "hi"}), msgpack_installed=False)

    assert message.headers["Content-Type"].startswith(JSON_CT)
    json.loads(message.response)  # raises if the body is not what the header says


# --- the client side --------------------------------------------------------


def test_the_client_turns_that_answer_into_a_documented_exception():
    """A reply the client cannot classify is barely better than no reply."""
    client = ServiceClient(service="svc", verify=False)
    envelope = {
        "success": False,
        "error": (
            "unsupported serialization format: MessagePack serialization requires the "
            "'msgpack' package. Install it with: pip install 'cliffracer[msgpack]'"
        ),
    }

    with pytest.raises(RpcServerError) as caught:
        client._raise_for_error(envelope, "svc.ping")

    assert isinstance(caught.value, RpcError)
    assert not isinstance(caught.value, RpcClientError), (
        "the service's missing dependency is not the caller's error to fix"
    )
    assert "cliffracer[msgpack]" in str(caught.value)


# --- what must not change ---------------------------------------------------


async def test_a_json_request_is_untouched():
    """The control: the fallback must not change the ordinary path."""
    body = json.dumps({"value": "hi"}).encode()
    message = await _drive(JSON_CT, body, msgpack_installed=False)
    envelope = json.loads(message.response)

    assert envelope["success"] is True
    assert envelope["result"] == {"echo": "hi"}
    assert message.headers["Content-Type"].startswith(JSON_CT)


async def test_a_msgpack_request_still_gets_msgpack_when_the_extra_is_there():
    """The other control: nothing downgrades a service that can write msgpack.

    Without this, replying in JSON always would satisfy every assertion above.
    """
    message = await _drive(MSGPACK_CT, msgpack.packb({"value": "hi"}), msgpack_installed=True)
    envelope = msgpack.unpackb(message.response)

    assert envelope["success"] is True
    assert envelope["result"] == {"echo": "hi"}
    assert message.headers["Content-Type"].startswith(MSGPACK_CT)


async def test_a_request_with_no_reply_subject_does_not_raise():
    """A fire-and-forget request has nobody to answer, and must not fail trying."""
    message = FakeMsg(msgpack.packb({"value": "hi"}), MSGPACK_CT)
    message.reply = ""
    service = Svc(ServiceConfig(name="svc", health_listener=False))
    service.container.discover_handlers()
    saved = validation.msgpack
    validation.msgpack = None
    try:
        await service.container.dispatcher.handle_rpc_request(message)
    finally:
        validation.msgpack = saved

    assert message.response is None
