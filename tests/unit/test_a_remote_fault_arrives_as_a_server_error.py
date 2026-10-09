"""A fault on the remote side reaches the caller as `RpcServerError`.

`RpcServerError` is defined, aliased as `RpcRemoteError`, exported from both
`cliffracer.client` and `cliffracer`, and documented as "remote service raised
an unhandled exception or returned an error envelope". Nothing raised it. The
branch that handles exactly that case -- the fall-through in
`_raise_for_error`, which is where the server's `Internal server error`
envelope lands -- raised `ClientError`, the alias for `RpcClientError`, whose
own docstring claims the opposite half: "client-side invocation, validation, or
transport failures".

So the documented split was not observable at runtime. A caller writing
`except RpcServerError` to spot a remote 500 caught nothing ever, and one
writing `except RpcClientError` to fix its own arguments swallowed remote 500s
silently. The existing tests asserted the class existed and subclassed
`RpcError`; none asserted anything produced it.

The two sites that move are the ones where the REMOTE misbehaved. The two in
`verify()` do not: a client with no `SIGNATURES`, and a subject served by a
different service, are both the caller's own mistake and stay `RpcClientError`.
That distinction is asserted here, because a fix that simply swapped every
`ClientError` in the file would pass a test that only looked at the fall-through.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import (
    RpcClientError,
    RpcError,
    RpcRefusedError,
    RpcServerError,
    RpcUnknownMethodError,
    RpcValidationError,
)

pytestmark = pytest.mark.unit

# The envelope the dispatcher answers an unhandled handler exception with,
# copied from src/cliffracer/core/dispatch/rpc.py rather than invented.
INTERNAL_ERROR = {
    "success": False,
    "error": "Internal server error (correlation_id: abc)",
    "timestamp": "2026-01-01T00:00:00+00:00",
    "correlation_id": "abc",
}


def a_client() -> ServiceClient:
    return ServiceClient(service="svc", verify=False)


def test_an_unhandled_handler_exception_reaches_the_caller_as_a_server_error():
    """The dispatcher's own internal-error envelope, through the real branch."""
    with pytest.raises(RpcServerError) as caught:
        a_client()._raise_for_error(INTERNAL_ERROR, "svc.rpc.do")

    assert "Internal server error (correlation_id: abc)" in str(caught.value)


def test_an_unrecognised_error_envelope_is_attributed_to_the_remote():
    """Anything the service says that is not one of the known shapes is its fault."""
    with pytest.raises(RpcServerError):
        a_client()._raise_for_error({"success": False, "error": "boom"}, "svc.rpc.do")


async def test_a_reply_that_carries_no_success_key_is_a_server_error():
    """A malformed reply is the remote breaking the protocol, not the caller."""
    client = a_client()
    client._nc = AsyncMock()
    reply = SimpleNamespace(data=json.dumps({"result": 1}).encode(), headers=None)

    with patch.object(ServiceClient, "_request", AsyncMock(return_value=reply)):
        with pytest.raises(RpcServerError) as caught:
            await client._call("do", {}, int)

    assert "carries no success key" in str(caught.value)


@pytest.mark.parametrize(
    ("envelope", "expected"),
    [
        ({"success": False, "error": "validation failed", "details": []}, RpcValidationError),
        ({"error": "Unknown method: nope"}, RpcUnknownMethodError),
        ({"error": "refused: rate limited"}, RpcRefusedError),
    ],
    ids=["validation", "unknown_method", "refused"],
)
def test_the_envelopes_the_caller_can_act_on_stay_client_errors(envelope, expected):
    """Moving the fall-through must not drag the recognised shapes with it."""
    with pytest.raises(expected) as caught:
        a_client()._raise_for_error(envelope, "svc.rpc.do")

    assert isinstance(caught.value, RpcClientError)
    assert not isinstance(caught.value, RpcServerError)


async def test_a_client_with_no_signatures_is_still_the_callers_own_mistake():
    """`verify()` without SIGNATURES is client-side and must not become a server error."""
    client = ServiceClient(service="svc")

    with pytest.raises(RpcClientError) as caught:
        await client.verify()

    assert not isinstance(caught.value, RpcServerError)
    assert "declares no signatures" in str(caught.value)


async def test_a_subject_served_by_another_service_is_the_callers_own_mistake():
    """A `service=`/`namespace=` slip is the caller's, however the remote answers."""
    client = ServiceClient(service="svc", verify=False)
    client.SIGNATURES = {"do": "sha256:x"}
    client._nc = AsyncMock()
    described = {
        "service": "somethingelse",
        "version": "1",
        "description_hash": "sha256:d",
        "methods": [],
    }
    reply = SimpleNamespace(data=json.dumps(described).encode(), headers=None)

    with patch.object(ServiceClient, "_request", AsyncMock(return_value=reply)):
        with pytest.raises(RpcClientError) as caught:
            await client.verify()

    assert not isinstance(caught.value, RpcServerError)
    assert "is served by" in str(caught.value)


def test_the_two_halves_are_disjoint_so_a_caller_can_tell_them_apart():
    """Neither catches the other; `RpcError` catches both."""
    assert not issubclass(RpcServerError, RpcClientError)
    assert not issubclass(RpcClientError, RpcServerError)
    assert issubclass(RpcServerError, RpcError)
    assert issubclass(RpcClientError, RpcError)
