"""Gap fixes: typed RPC errors with .details, correlation in NATS headers, in-process propagation."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.exceptions import RPCError, RPCTimeoutError


def _caller(namespace=None):
    svc = CliffracerService(ServiceConfig(name="caller", namespace=namespace))
    svc.nc = AsyncMock()
    return svc


def _resp(payload: dict):
    r = AsyncMock()
    r.data = json.dumps(payload).encode()
    return r


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_rpc_raises_rpc_error_with_details():
    svc = _caller()
    svc.nc.request.return_value = _resp(
        {
            "success": False,
            "error": "validation failed",
            "details": [{"loc": ["email"], "msg": "field required"}],
        }
    )
    with pytest.raises(RPCError) as exc:
        await svc.call_rpc("user_service", "create_user", username="x")
    assert exc.value.details == [{"loc": ["email"], "msg": "field required"}]
    assert "validation failed" in str(exc.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_rpc_timeout_raises_rpc_timeout_error():
    # nats raises nats.errors.TimeoutError on request timeout (what call_rpc catches)
    from nats.errors import TimeoutError as NATSTimeoutError

    svc = _caller()
    svc.nc.request.side_effect = NATSTimeoutError()
    with pytest.raises(RPCTimeoutError):
        await svc.call_rpc("user_service", "get_user")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_rpc_success_returns_result():
    svc = _caller()
    svc.nc.request.return_value = _resp({"success": True, "result": {"id": "u1"}})
    assert await svc.call_rpc("user_service", "get_user") == {"id": "u1"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_rpc_sets_correlation_header():
    svc = _caller()
    svc.nc.request.return_value = _resp({"success": True, "result": None})
    CorrelationContext.set("trace-abc")
    await svc.call_rpc("user_service", "get_user")
    assert svc.nc.request.call_args.kwargs["headers"]["correlation_id"] == "trace-abc"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_event_sets_correlation_header():
    svc = _caller()
    CorrelationContext.set("trace-xyz")
    await svc.publish_event("orders.created", n=1)
    assert svc.nc.publish.call_args.kwargs["headers"]["correlation_id"] == "trace-xyz"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correlation_propagates_into_spawned_task():
    """Regression lock-in: asyncio.create_task inherits the correlation contextvar
    (Python copies the context at task creation). Threads/run_in_executor are the
    documented exception, not covered here."""
    CorrelationContext.set("trace-task")
    seen = []

    async def child():
        seen.append(CorrelationContext.get())

    await asyncio.create_task(child())
    assert seen == ["trace-task"]
