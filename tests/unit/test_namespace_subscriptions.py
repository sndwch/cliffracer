"""RPC/async subscriptions are namespaced + queue-grouped; events are not."""

from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener

pytestmark = pytest.mark.unit


def _svc(namespace=None):
    class S(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    svc = S(ServiceConfig(name="user_service", namespace=namespace))
    svc._discover_handlers()
    svc.nc = AsyncMock()
    svc._running = False  # so subscription-handler tasks exit immediately
    return svc


@pytest.mark.asyncio
async def test_rpc_async_subscriptions_namespaced_and_queued():
    svc = _svc(namespace="app1")
    await svc.container.setup_subscriptions()
    calls = {c.args[0]: c.kwargs for c in svc.nc.subscribe.call_args_list}
    assert "app1.user_service.rpc.*" in calls
    assert calls["app1.user_service.rpc.*"]["queue"] == "app1.user_service.rpc"
    assert "app1.user_service.async.*" in calls
    assert calls["app1.user_service.async.*"]["queue"] == "app1.user_service.async"


@pytest.mark.asyncio
async def test_event_subscription_has_no_queue_group():
    svc = _svc(namespace="app1")
    await svc.container.setup_subscriptions()
    # the event subscription (namespace-local subject) must NOT pass a queue
    event_calls = [c for c in svc.nc.subscribe.call_args_list if "orders.created" in c.args[0]]
    assert event_calls, "expected an event subscription"
    for c in event_calls:
        assert "queue" not in c.kwargs


@pytest.mark.asyncio
async def test_backcompat_no_namespace_subjects_unchanged():
    svc = _svc(namespace=None)
    await svc.container.setup_subscriptions()
    subjects = [c.args[0] for c in svc.nc.subscribe.call_args_list]
    assert "user_service.rpc.*" in subjects  # unchanged subject
    assert "user_service.async.*" in subjects
    # queue group still added (transparent for single instance)
    rpc_call = next(c for c in svc.nc.subscribe.call_args_list if c.args[0] == "user_service.rpc.*")
    assert rpc_call.kwargs["queue"] == "user_service.rpc"
