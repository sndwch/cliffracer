import asyncio
import gc
import json
import weakref
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, RpcProxy, ServiceConfig

pytestmark = pytest.mark.unit


def test_rpc_proxy_does_not_leak_service_instance():
    class LeakTestService(CliffracerService):
        target = RpcProxy("target")

    svc = LeakTestService(ServiceConfig(name="test"))
    proxy = svc.target
    assert proxy is not None

    ref = weakref.ref(svc)
    del svc
    # `proxy` stays alive on purpose: a ServiceProxy (and the MethodProxy it hands out) holds only
    # a weak reference to its service, and the per-instance cache is a WeakKeyDictionary, so a
    # proxy that outlives the service does not keep it alive.
    gc.collect()
    assert ref() is None
    assert proxy is not None


class TestRpcProxyUnit:
    """Unit tests for RpcProxy without NATS."""

    def test_rpc_proxy_descriptor(self):
        """Test that RpcProxy works as a descriptor."""

        class TestService(CliffracerService):
            other = RpcProxy("other_service")

            def __init__(self):
                config = ServiceConfig(name="test")
                super().__init__(config)

        service = TestService()

        # Should get a ServiceProxy instance
        proxy = service.other
        assert hasattr(proxy, "_service_name")
        assert proxy._service_name == "other_service"

    def test_rpc_proxy_caching(self):
        """Test that ServiceProxy is cached per instance."""

        class TestService(CliffracerService):
            other = RpcProxy("other_service")

            def __init__(self):
                config = ServiceConfig(name="test")
                super().__init__(config)

        service = TestService()

        # Multiple accesses should return the same ServiceProxy
        proxy1 = service.other
        proxy2 = service.other
        assert proxy1 is proxy2

    def test_method_proxy_creation(self):
        """Test that MethodProxy is created for method access."""

        class TestService(CliffracerService):
            other = RpcProxy("other_service")

            def __init__(self):
                config = ServiceConfig(name="test")
                super().__init__(config)

        service = TestService()

        # Accessing a method should give us a MethodProxy
        method = service.other.some_method
        assert hasattr(method, "_service_name")
        assert hasattr(method, "_method_name")
        assert method._service_name == "other_service"
        assert method._method_name == "some_method"


def test_a_proxy_that_outlives_its_service_refuses_by_name():
    """The three places a proxy reads its service all say so, rather than failing on None."""

    class Owner(CliffracerService):
        target = RpcProxy("target")

    svc = Owner(ServiceConfig(name="test"))
    proxy = svc.target
    method = proxy.do_thing
    del svc
    gc.collect()

    with pytest.raises(RuntimeError, match="Service instance was garbage collected"):
        proxy.another_method  # noqa: B018 - the lookup is the thing under test
    with pytest.raises(RuntimeError, match="Service instance was garbage collected"):
        method.call_async(item="x")

    async def call_it():
        await method(item="x")

    with pytest.raises(RuntimeError, match="Service instance was garbage collected"):
        asyncio.run(call_it())


def test_a_private_name_on_a_proxy_is_an_attribute_error_not_a_method():
    class Owner(CliffracerService):
        target = RpcProxy("target")

    owner = Owner(ServiceConfig(name="test"))

    with pytest.raises(AttributeError):
        owner.target._private  # noqa: B018 - the lookup is the thing under test
    assert owner.target.public._method_name == "public"


class _Caller(CliffracerService):
    other = RpcProxy("other_service")
    elsewhere = RpcProxy("other_service", namespace="app2")


def _caller(namespace: str | None = None) -> _Caller:
    svc = _Caller(ServiceConfig(name="caller", namespace=namespace))
    svc.nc = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_proxy_call_requests_the_rpc_subject_with_its_arguments_and_returns_the_result():
    """The request/reply path of a proxy: `service.rpc.method`, the keyword arguments as the
    payload, and the reply's `result` handed back."""
    svc = _caller()
    reply = AsyncMock()
    reply.data = json.dumps({"success": True, "result": 42}).encode()
    svc.nc.request = AsyncMock(return_value=reply)

    result = await svc.other.compute(x=1, y="two")

    assert result == 42
    svc.nc.request.assert_awaited_once()
    subject, body = svc.nc.request.await_args.args[:2]
    assert subject == "other_service.rpc.compute"
    assert (json.loads(body)["x"], json.loads(body)["y"]) == (1, "two")
    svc.nc.publish.assert_not_called()


@pytest.mark.asyncio
async def test_proxy_call_honours_the_proxy_namespace_override():
    svc = _caller(namespace="app1")
    reply = AsyncMock()
    reply.data = json.dumps({"success": True, "result": None}).encode()
    svc.nc.request = AsyncMock(return_value=reply)

    await svc.elsewhere.compute(x=1)

    assert svc.nc.request.await_args.args[0] == "app2.other_service.rpc.compute"


@pytest.mark.asyncio
async def test_proxy_call_async_publishes_to_the_async_subject():
    """A proxy fire-and-forget call reaches the callee's async budget, not its request/reply one."""
    svc = _caller()

    await svc.other.do_thing.call_async(item="widget")

    svc.nc.publish.assert_called_once()
    subject, body = svc.nc.publish.call_args.args[:2]
    assert subject == "other_service.async.do_thing"
    assert json.loads(body)["item"] == "widget"
    svc.nc.request.assert_not_called()


@pytest.mark.asyncio
async def test_proxy_call_async_and_service_call_async_agree_on_the_subject():
    svc = _caller(namespace="app1")

    await svc.other.do_thing.call_async(item="widget")
    await svc.call_async("other_service", "do_thing", item="widget")

    proxy_subject, service_subject = (c.args[0] for c in svc.nc.publish.call_args_list)
    assert proxy_subject == service_subject == "app1.other_service.async.do_thing"


@pytest.mark.asyncio
async def test_proxy_call_async_honours_the_proxy_namespace_override():
    svc = _caller(namespace="app1")

    await svc.elsewhere.do_thing.call_async(item="widget")

    assert svc.nc.publish.call_args.args[0] == "app2.other_service.async.do_thing"
