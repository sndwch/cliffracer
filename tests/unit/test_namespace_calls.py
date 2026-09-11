"""Outbound RPC/event subjects honor the caller's namespace and explicit overrides."""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, RpcProxy, ServiceConfig


class _Order(BaseModel):
    order_id: str
    amount: float


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dead_letter_subject_is_single_prefixed():
    """A namespaced service dead-letters to dlq.{service} by default without namespace prefixing."""
    from pydantic import ValidationError

    svc = CliffracerService(ServiceConfig(name="order_svc", namespace="app1"))
    svc.nc = AsyncMock()  # mock the low-level publish so we observe the real prefixing
    try:
        _Order(order_id="o1")  # missing amount -> real ValidationError
    except ValidationError as e:
        err = e
    await svc.container.dispatcher._handle_invalid_message(
        "orders.created", {"order_id": "o1"}, err, _Order, None
    )
    published_subjects = [c.args[0] for c in svc.nc.publish.call_args_list]
    assert "dlq.order_svc" in published_subjects
    assert "app1.dlq.order_svc" not in published_subjects


def _svc(namespace=None):
    svc = CliffracerService(ServiceConfig(name="caller", namespace=namespace))
    svc.nc = AsyncMock()
    # make nc.request return a valid RPC response envelope
    resp = AsyncMock()
    resp.data = json.dumps({"result": "ok"}).encode()
    svc.nc.request.return_value = resp
    return svc


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_rpc_uses_caller_namespace():
    svc = _svc(namespace="app1")
    await svc.call_rpc("user_service", "get_user", user_id="u1")
    assert svc.nc.request.call_args.args[0] == "app1.user_service.rpc.get_user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_rpc_explicit_namespace_override():
    svc = _svc(namespace="app1")
    await svc.call_rpc("user_service", "get_user", namespace="app2", user_id="u1")
    assert svc.nc.request.call_args.args[0] == "app2.user_service.rpc.get_user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_rpc_no_namespace_unchanged():
    svc = _svc(namespace=None)
    await svc.call_rpc("user_service", "get_user", user_id="u1")
    assert svc.nc.request.call_args.args[0] == "user_service.rpc.get_user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_event_namespaced():
    svc = _svc(namespace="app1")
    await svc.publish_event("orders.created", order_id="o1")
    assert svc.nc.publish.call_args.args[0] == "app1.orders.created"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rpc_proxy_threads_namespace():
    class _Holder(CliffracerService):
        other = RpcProxy("user_service", namespace="app2")

    holder = _Holder(ServiceConfig(name="holder", namespace="app1"))
    holder.nc = AsyncMock()
    resp = AsyncMock()
    resp.data = json.dumps({"result": "ok"}).encode()
    holder.nc.request.return_value = resp
    await holder.other.get_user(user_id="u1")
    assert holder.nc.request.call_args.args[0] == "app2.user_service.rpc.get_user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_rpc_positional_payload_rejected():
    svc = _svc(namespace="app1")
    with pytest.raises(TypeError):
        await svc.call_rpc("user_service", "get_user", {"payload": 1})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_rpc_no_wait_positional_payload_rejected():
    svc = _svc(namespace="app1")
    with pytest.raises(TypeError):
        await svc.call_rpc_no_wait("user_service", "get_user", {"payload": 1})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_rpc_whitespace_subject_raises_before_wire():
    svc = _svc(namespace="app1")
    with pytest.raises(ValueError, match="whitespace"):
        await svc.call_rpc("user service", "get_user", user_id="u1")
