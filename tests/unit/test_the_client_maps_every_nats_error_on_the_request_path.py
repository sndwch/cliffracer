"""Every nats-py failure on the request path reaches the caller as an `RpcError`.

The API reference says every failure on the request path arrives as one of the documented classes, and
`_request` caught `NatsTimeout`, `NoResponders`, `ConnectionClosedError` and `StaleConnectionError`
only. `MaxPayloadError` (an argument over the broker's `max_payload`), `OutboundBufferLimitError` (the
buffer is full during the reconnect gap) and `ConnectionDrainingError` reached the caller as nats-py
exceptions, which a caller's `except RpcError` does not hold: ordinary operational failures, an
oversized argument and a call made while the broker is away or the connection drains.
"""

from unittest.mock import AsyncMock

import pytest
from nats import errors

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import (
    RpcClientError,
    RpcConnectionError,
    RpcError,
    RpcNoRespondersError,
    RpcTimeoutError,
)

pytestmark = pytest.mark.unit


def _client(raised: BaseException) -> ServiceClient:
    client = ServiceClient(service="svc", verify=False)
    client._nc = AsyncMock()
    client._nc.request.side_effect = raised
    return client


NEWLY_MAPPED = [
    pytest.param(errors.MaxPayloadError(), RpcClientError, "max_payload", id="over-max-payload"),
    pytest.param(
        errors.OutboundBufferLimitError(), RpcConnectionError, "buffer is full", id="buffer-full"
    ),
    pytest.param(errors.ConnectionDrainingError(), RpcConnectionError, "draining", id="draining"),
    pytest.param(errors.BadSubjectError(), RpcConnectionError, "BadSubjectError", id="bad-subject"),
    pytest.param(errors.NoServersError(), RpcConnectionError, "NoServersError", id="no-servers"),
    pytest.param(errors.Error(), RpcConnectionError, "Error", id="the-base-class"),
]


@pytest.mark.parametrize(("raised", "expected", "says"), NEWLY_MAPPED)
async def test_a_nats_error_is_an_rpc_error_of_the_documented_class(raised, expected, says):
    client = _client(raised)

    with pytest.raises(RpcError) as caught:
        await client._request("svc.rpc.m", b"{}")

    assert type(caught.value) is expected
    assert says in str(caught.value) and "svc.rpc.m" in str(caught.value)
    assert caught.value.__cause__ is raised, "the nats-py error is kept as the cause"


async def test_an_oversized_argument_is_the_callers_and_not_a_connection_failure():
    client = _client(errors.MaxPayloadError())

    with pytest.raises(RpcClientError) as caught:
        await client._request("svc.rpc.m", b"x")

    assert not isinstance(caught.value, RpcConnectionError)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        pytest.param(errors.TimeoutError(), RpcTimeoutError, id="timeout"),
        pytest.param(errors.NoRespondersError(), RpcNoRespondersError, id="no-responders"),
        pytest.param(errors.ConnectionClosedError(), RpcConnectionError, id="closed"),
        pytest.param(errors.StaleConnectionError(), RpcConnectionError, id="stale"),
    ],
)
async def test_CONTROL_the_four_already_mapped_keep_their_classes(raised, expected):
    client = _client(raised)

    with pytest.raises(RpcError) as caught:
        await client._request("svc.rpc.m", b"{}")

    assert type(caught.value) is expected


async def test_CONTROL_an_error_that_is_not_nats_py_s_is_not_swallowed():
    client = _client(RuntimeError("a bug in the caller's own code"))

    with pytest.raises(RuntimeError):
        await client._request("svc.rpc.m", b"{}")


# --- a ConnectionReconnectingError, on the call path and on the describe path -------------------------


async def test_a_call_made_while_the_connection_is_reconnecting_is_an_rpc_connection_error():
    """nats-py refuses a request while it redials, and the call path used to let that escape."""
    raised = errors.ConnectionReconnectingError()
    client = _client(raised)

    with pytest.raises(RpcConnectionError) as caught:
        await client._call("m", {}, int)

    assert caught.value.__cause__ is raised, "the nats-py error is kept as the cause"
    assert "svc.rpc.m" in str(caught.value)


async def test_verify_while_the_connection_is_reconnecting_is_an_rpc_connection_error():
    """The describe request is a request like any other: the same error, from the same place."""
    raised = errors.ConnectionReconnectingError()
    client = _client(raised)
    client.SIGNATURES = {"m": "sha256:x"}

    with pytest.raises(RpcConnectionError) as caught:
        await client.verify()

    assert caught.value.__cause__ is raised, "the nats-py error is kept as the cause"
    assert "svc.describe" in str(caught.value)
