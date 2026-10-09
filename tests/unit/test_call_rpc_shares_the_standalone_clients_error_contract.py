"""The service's `call_rpc` raises what the standalone client raises, for the same reply.

An error envelope reached `call_rpc`'s caller as a plain `RpcError` while the standalone client
raised the typed member of the hierarchy for it, and a connection lost mid-call reached the caller as
nats' own `ConnectionClosedError` or `StaleConnectionError`, outside `except RpcError`, so neither
the caller's RPC handling nor the circuit breaker saw it. One function now reads the envelope for
both, and the lost connection is an `RpcConnectionError` with the nats error as its cause.
"""

import json
from unittest.mock import AsyncMock

import nats.errors
import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import (
    RpcClientError,
    RpcConnectionError,
    RpcError,
    RpcRefusedError,
    RpcServerError,
    RpcUnknownMethodError,
    RpcValidationError,
)

pytestmark = pytest.mark.unit

SUBJECT = "user_service.rpc.create_user"
DETAILS = [{"loc": ["email"], "msg": "field required"}]

# (the reply envelope, the class both paths raise for it)
ENVELOPES = {
    "coded validation": (
        {
            "success": False,
            "code": "validation_failed",
            "error": "validation failed",
            "details": DETAILS,
        },
        RpcValidationError,
    ),
    "coded unknown method": (
        {"success": False, "code": "unknown_method", "error": "Unknown method: nope"},
        RpcUnknownMethodError,
    ),
    "coded refusal": (
        {"success": False, "code": "refused", "error": "refused: rate limit exceeded"},
        RpcRefusedError,
    ),
    "coded, a code this caller does not know": (
        {"success": False, "code": "from_the_future", "error": "something new"},
        RpcServerError,
    ),
    "a crash that reads like a refusal, under its own code": (
        {"success": False, "code": "internal_error", "error": "refused: the handler's own text"},
        RpcServerError,
    ),
    "uncoded validation": (
        {"success": False, "error": "validation failed", "details": DETAILS},
        RpcValidationError,
    ),
    "uncoded unknown method": ({"error": "Unknown method: nope"}, RpcUnknownMethodError),
    "uncoded refusal": ({"error": "refused: policy"}, RpcRefusedError),
    "uncoded server fault": (
        {"error": "Internal server error (correlation_id: abc)"},
        RpcServerError,
    ),
}


def _caller() -> CliffracerService:
    svc = CliffracerService(ServiceConfig(name="caller"))
    svc.nc = AsyncMock()
    return svc


def _reply(payload: dict) -> AsyncMock:
    reply = AsyncMock()
    reply.data = json.dumps(payload).encode()
    return reply


async def _raised_by_call_rpc(envelope: dict) -> BaseException:
    svc = _caller()
    svc.nc.request.return_value = _reply(envelope)
    with pytest.raises(BaseException) as caught:  # noqa: PT011 - the class is what is asserted
        await svc.call_rpc("user_service", "create_user", username="x")
    return caught.value


def _raised_by_the_client(envelope: dict) -> BaseException:
    client = ServiceClient(service="user_service", verify=False)
    with pytest.raises(BaseException) as caught:  # noqa: PT011
        client._raise_for_error(envelope, SUBJECT)
    return caught.value


@pytest.mark.parametrize("name", ENVELOPES)
async def test_an_error_envelope_raises_the_typed_member_of_the_hierarchy(name):
    envelope, expected = ENVELOPES[name]

    raised = await _raised_by_call_rpc(envelope)

    assert type(raised) is expected, (name, type(raised))
    assert isinstance(raised, RpcError)


@pytest.mark.parametrize("name", ENVELOPES)
async def test_call_rpc_and_the_standalone_client_raise_the_same_class_and_message(name):
    envelope, _ = ENVELOPES[name]

    from_call_rpc = await _raised_by_call_rpc(envelope)
    from_the_client = _raised_by_the_client(envelope)

    assert type(from_call_rpc) is type(from_the_client)
    assert str(from_call_rpc) == str(from_the_client)


async def test_a_validation_envelope_carries_its_details_and_is_a_client_error():
    raised = await _raised_by_call_rpc(ENVELOPES["coded validation"][0])

    assert isinstance(raised, RpcClientError)
    assert raised.details == DETAILS


async def test_a_server_fault_names_the_subject_and_is_not_a_client_error():
    raised = await _raised_by_call_rpc({"error": "Internal server error (correlation_id: abc)"})

    assert not isinstance(raised, RpcClientError)
    assert SUBJECT in str(raised)
    assert "Internal server error" in str(raised)


async def test_a_reply_with_no_error_still_returns_its_result():
    svc = _caller()
    svc.nc.request.return_value = _reply({"success": True, "result": {"id": "u1"}})

    assert await svc.call_rpc("user_service", "create_user", username="x") == {"id": "u1"}


# ---- a connection lost mid-call ---------------------------------------------------------------


@pytest.mark.parametrize(
    "lost", [nats.errors.ConnectionClosedError, nats.errors.StaleConnectionError]
)
async def test_a_connection_lost_mid_call_is_an_rpc_connection_error_with_the_cause(lost):
    svc = _caller()
    svc.nc.request.side_effect = lost()

    with pytest.raises(RpcConnectionError) as caught:
        await svc.call_rpc("user_service", "create_user", username="x")

    assert isinstance(caught.value, RpcError), "so `except RpcError` and the breaker see it"
    assert isinstance(caught.value.__cause__, lost)
    assert SUBJECT in str(caught.value)


async def test_the_standalone_client_wraps_the_same_two_errors_the_same_way():
    for lost in (nats.errors.ConnectionClosedError, nats.errors.StaleConnectionError):
        client = ServiceClient(service="user_service", verify=False)
        client._nc = AsyncMock()
        client._nc.request = AsyncMock(side_effect=lost())

        with pytest.raises(RpcConnectionError) as caught:
            await client._request(SUBJECT, b"{}", {})

        assert isinstance(caught.value.__cause__, lost)


async def test_the_send_hooks_see_the_wrapped_error_not_the_nats_one():
    """`after_call` is where tracing and the circuit's accounting read the outcome."""
    from cliffracer.core.extension import Extension

    seen: list[BaseException | None] = []

    class Watcher(Extension):
        async def after_call(self, ctx, result, exc):
            seen.append(exc)

    class Svc(CliffracerService):
        watcher = Watcher()

    svc = Svc(ServiceConfig(name="caller", health_port=0))
    await svc.container._setup_extensions()
    svc.nc = AsyncMock()
    svc.nc.request.side_effect = nats.errors.ConnectionClosedError()

    with pytest.raises(RpcConnectionError):
        await svc.call_rpc("user_service", "create_user", username="x")

    assert len(seen) == 1 and isinstance(seen[0], RpcConnectionError)
