"""An RPC handler is callable through the framework's own proxy, or it does not start.

`call_rpc`, `call_async` and `call_rpc_no_wait` take the routing namespace as `namespace=` and
collect the remote arguments as `**kwargs`. A handler with a parameter called `namespace` was
legal, startable and describable, and the recommended calling convention, `RpcProxy`, raised a
`TypeError` that named an internal parameter. The name is refused with the handlers' other
unusable names, at startup and in `describe`. `service` and `method`, the other names the call
methods used to take by keyword, are positional-only now and so reach the wire as arguments.
"""

import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, RpcProxy, ServiceConfig, async_rpc, rpc
from cliffracer.core.typed_rpc import UntypedHandler
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit


def _with_namespace_parameter():
    class S(CliffracerService):
        @rpc
        async def find(self, namespace: str) -> str:
            return namespace

    return S


def _async_with_namespace_parameter():
    class S(CliffracerService):
        @async_rpc
        async def find(self, namespace: str) -> None: ...

    return S


@pytest.mark.parametrize("build", [_with_namespace_parameter, _async_with_namespace_parameter])
def test_a_namespace_parameter_is_refused_at_startup_naming_the_clash(build):
    svc = build()(ServiceConfig(name="s", health_port=0))

    with pytest.raises(UntypedHandler, match=r"S\.find.*'namespace'.*routing argument"):
        svc._discover_handlers()


def test_describe_refuses_it_too():
    with pytest.raises(UntypedHandler, match="'namespace'"):
        describe(_with_namespace_parameter(), service="s", version="1")


def test_CONTROL_a_parameter_that_only_resembles_the_routing_name_is_accepted():
    class S(CliffracerService):
        @rpc
        async def find(self, namespaces: str, ns: str) -> str:
            return namespaces + ns

    svc = S(ServiceConfig(name="s", health_port=0))
    svc._discover_handlers()

    assert "find" in svc.container.registry.rpc_handlers


def _caller():
    svc = CliffracerService(ServiceConfig(name="caller", health_port=0))
    svc.nc = AsyncMock()
    reply = AsyncMock()
    reply.data = json.dumps({"success": True, "result": "ok"}).encode()
    svc.nc.request.return_value = reply
    return svc


@pytest.mark.parametrize("name", ["service", "method"])
async def test_a_remote_argument_named_like_a_positional_routing_argument_reaches_the_wire(name):
    svc = _caller()

    await svc.call_rpc("peer", "find", **{name: "value"})

    assert svc.nc.request.call_args.args[0] == "peer.rpc.find"
    sent = json.loads(svc.nc.request.call_args.args[1])
    assert sent[name] == "value"


async def test_the_proxy_passes_a_remote_argument_named_service_or_method():
    class Caller(CliffracerService):
        peer = RpcProxy("peer")

    svc = Caller(ServiceConfig(name="caller", health_port=0))
    svc.nc = AsyncMock()
    reply = AsyncMock()
    reply.data = json.dumps({"success": True, "result": "ok"}).encode()
    svc.nc.request.return_value = reply

    await svc.peer.find(service="a", method="b")

    sent = json.loads(svc.nc.request.call_args.args[1])
    assert (sent["service"], sent["method"]) == ("a", "b")


async def test_call_async_and_call_rpc_no_wait_take_the_same_names_as_arguments():
    svc = _caller()
    svc.nc.publish = AsyncMock()

    await svc.call_async("peer", "find", service="a", method="b")
    await svc.call_rpc_no_wait("peer", "find", service="c", method="d")

    sent = [json.loads(call.args[1]) for call in svc.nc.publish.call_args_list]
    assert [(m["service"], m["method"]) for m in sent] == [("a", "b"), ("c", "d")]
