"""Every failure on the request path arrives as a documented class.

`_request`'s docstring promises to "translate network errors into ClientError
exceptions" and the API reference says the client maps a reply to one of a
named set. Neither was true past the point `nc.request` returned: everything
after it trusted the wire, and ten distinct classes escaped.

ENUMERATED, NOT ASSUMED. Driving the real `_call` and `verify()` with fake
replies on the unfixed code gave:

    ENCODE  unserialisable param     builtins.TypeError
    DECODE  undecodable bytes        builtins.UnicodeDecodeError
    DECODE  not json                 json.decoder.JSONDecodeError
    DECODE  scalar reply             builtins.TypeError
    DECODE  json array               builtins.AttributeError
    DECODE  json null                builtins.TypeError
    RESULT  wrong result type        pydantic_core.ValidationError
    VERIFY  foreign json object      builtins.KeyError
    VERIFY  json array               builtins.TypeError
    VERIFY  not json                 json.decoder.JSONDecodeError
    VERIFY  undecodable              builtins.UnicodeDecodeError

none of them a `CliffracerError`. Two of those are not in the issue -- an
argument `json.dumps` cannot serialise, and a `result` that does not match the
declared return type -- and the issue records the array case as `TypeError`
where it is actually `AttributeError`. The list above is what the code does.

WHICH SIDE EACH FAILURE IS ATTRIBUTED TO. An argument this client cannot encode
is the caller's, so it is an `RpcClientError`. A reply that cannot be decoded,
is not a JSON object, or carries a `result` that does not match the declared
return type is the remote breaking the contract, so it is an `RpcServerError` --
the same split error envelopes use. A describe answered by
something that is not a Cliffracer service is an `RpcClientError`, because the
actionable cause is a `service=`/`namespace=` slip and that is what the
existing (previously unreachable) service-name guard beside it already raises.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import (
    ClientError,
    CliffracerError,
    RpcClientError,
    RpcError,
    RpcServerError,
)

pytestmark = pytest.mark.unit


class Receipt(BaseModel):
    sku: str


def _reply(data: bytes, headers=None):
    return SimpleNamespace(data=data, headers=headers)


def _client(**kw) -> ServiceClient:
    client = ServiceClient(service="svc", verify=False, **kw)
    client._nc = AsyncMock()
    return client


async def _call_with(reply_bytes: bytes, return_type=int, params=None):
    client = _client()
    with patch.object(ServiceClient, "_request", AsyncMock(return_value=_reply(reply_bytes))):
        return await client._call("do", params if params is not None else {}, return_type)


# --- the reply cannot be read at all ----------------------------------------


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("undecodable bytes", b"\xff\xfe\x00"),
        ("not json", b"<html>nope</html>"),
        ("scalar", b"42"),
        ("json array", b"[1,2,3]"),
        ("json null", b"null"),
    ],
)
async def test_a_reply_that_is_not_an_object_is_a_server_error(name, payload):
    """Each of these escaped as a different builtin. They are one failure."""
    with pytest.raises(RpcServerError) as caught:
        await _call_with(payload)

    assert "svc.rpc.do" in str(caught.value), str(caught.value)


async def test_the_refusal_shows_what_came_back():
    """A reply nobody can parse is undiagnosable without a sight of it."""
    with pytest.raises(RpcServerError) as caught:
        await _call_with(b"<html>not json at all</html>")

    assert "html" in str(caught.value), str(caught.value)


# --- the reply is an object but the result is the wrong shape ---------------


async def test_a_result_that_does_not_match_the_declared_type_is_a_server_error():
    """pydantic's ValidationError is not in the documented family."""
    with pytest.raises(RpcServerError) as caught:
        await _call_with(b'{"success":true,"result":"abc"}', return_type=int)

    assert "svc.rpc.do" in str(caught.value), str(caught.value)


async def test_a_model_return_type_still_validates_the_reply():
    """The control for the line above: a good result is still parsed, not waved through."""
    got = await _call_with(b'{"success":true,"result":{"sku":"a"}}', return_type=Receipt)
    assert got == Receipt(sku="a")


# --- the ARGUMENTS cannot be encoded ----------------------------------------


async def test_an_argument_that_cannot_be_encoded_is_the_callers_error():
    """`json.dumps` raised a bare TypeError before the request left the process."""
    with pytest.raises(RpcClientError) as caught:
        await _call_with(b'{"success":true,"result":1}', params={"x": {1, 2}})

    message = str(caught.value)
    assert "svc.rpc.do" in message, message
    assert not isinstance(caught.value, RpcServerError), "nothing was sent; this is local"


# --- verify() ---------------------------------------------------------------


async def _verify_against(describe_bytes: bytes, headers=None):
    client = ServiceClient(service="svc")
    client._nc = AsyncMock()
    client.SIGNATURES = {"do": "sha256:x"}
    with patch.object(
        ServiceClient, "_request", AsyncMock(return_value=_reply(describe_bytes, headers))
    ):
        await client.verify()


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("a foreign json object", b'{"hello":"world"}'),
        ("a json array", b"[1,2,3]"),
        ("not json", b"<html>nope</html>"),
        ("undecodable", b"\xff\xfe\x00"),
    ],
)
async def test_a_describe_answered_by_something_else_names_the_subject(name, payload):
    """`{service}.describe` is a plain subject; anything may answer it.

    The service-name guard exists for exactly this and was unreachable, because
    `Description.from_dict` indexed the payload and raised first.

    Asserted on `RpcError` rather than on either half, deliberately. The client
    cannot tell a foreign responder from its own service answering badly, and
    an earlier version of this test demanded `RpcClientError` for all four --
    which would have meant attributing an unreadable reply to the caller on no
    evidence. What is guaranteed is a documented class that names the subject
    and shows what came back. The one shape the client CAN recognise -- a
    readable object that is not a description -- additionally carries the
    `service=`/`namespace=` hint, and `test_a_readable_reply_that_is_not_a_
    description_points_at_the_configuration` pins that.
    """
    with pytest.raises(RpcError) as caught:
        await _verify_against(payload)

    message = str(caught.value)
    assert "svc.describe" in message, message


async def test_a_readable_reply_that_is_not_a_description_points_at_the_configuration():
    """The case the unreachable guard was for: a namespace slip."""
    with pytest.raises(ClientError) as caught:
        await _verify_against(b'{"hello":"world"}')

    message = str(caught.value)
    assert "service=" in message and "namespace=" in message, message


async def test_CONTROL_a_real_description_still_verifies():
    """Otherwise "verify raises" could mean "verify always raises"."""
    from cliffracer.introspect import describe

    class Svc:
        pass

    good = {
        "service": "svc",
        "version": "1",
        "description_hash": "sha256:d",
        "methods": [{"name": "do", "signature_hash": "sha256:x", "params": [], "returns": None}],
    }
    client = ServiceClient(service="svc")
    client._nc = AsyncMock()
    client.SIGNATURES = {"do": "sha256:x"}
    with patch.object(
        ServiceClient, "_request", AsyncMock(return_value=_reply(json.dumps(good).encode()))
    ):
        await client.verify()
    assert client._verified is True
    assert describe is not None


async def test_verify_reads_the_reply_the_way_a_call_does():
    """`verify()` did its own `json.loads(...decode())`, ignoring content-type."""
    good = {
        "service": "svc",
        "version": "1",
        "description_hash": "sha256:d",
        "methods": [{"name": "do", "signature_hash": "sha256:x", "params": [], "returns": None}],
    }
    client = ServiceClient(service="svc")
    client._nc = AsyncMock()
    client.SIGNATURES = {"do": "sha256:x"}
    reply = _reply(json.dumps(good).encode(), headers={"Content-Type": "application/json"})
    with patch.object(ServiceClient, "_request", AsyncMock(return_value=reply)):
        await client.verify()
    assert client._verified is True


# --- the family ------------------------------------------------------------


async def test_CONTROL_a_normal_reply_is_untouched():
    """The whole point: none of this changes a call that works."""
    assert await _call_with(b'{"success":true,"result":7}', return_type=int) == 7


async def test_every_mapped_class_is_catchable_as_the_documented_family():
    """A caller writing `except RpcError` must contain all of it."""
    for payload in (b"\xff", b"42", b"[1]", b'{"success":true,"result":"abc"}'):
        with pytest.raises(RpcError):
            await _call_with(payload)
    assert issubclass(RpcServerError, CliffracerError)
    assert issubclass(ClientError, RpcClientError)
