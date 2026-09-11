"""Two same-named services in different namespaces don't cross-answer; cross_namespace spans both."""

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc


def _make_user_service(namespace):
    class UserService(CliffracerService):
        @rpc
        async def whoami(self) -> dict[str, str]:
            return {"namespace": namespace}

    return UserService(ServiceConfig(name="user_service", namespace=namespace))


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_namespaces_isolate_rpc_and_span_broadcasts():
    a = _make_user_service("appA")
    b = _make_user_service("appB")
    await a.start()
    await b.start()

    # a caller living in appA
    caller = CliffracerService(ServiceConfig(name="caller", namespace="appA"))
    await caller.start()

    # cross-namespace broadcast listener
    seen = []

    class Watcher(CliffracerService):
        @listener("orders.created", cross_namespace=True, fanout=True)
        async def on_order(self, subject: str, n: int = 0) -> None:
            seen.append(subject)

    watcher = Watcher(ServiceConfig(name="watcher", namespace="watch"))
    await watcher.start()
    await asyncio.sleep(0.2)

    try:
        # RPC stays within appA -> answered by the appA user_service only
        result = await caller.call_rpc("user_service", "whoami")
        assert result["namespace"] == "appA"

        # broadcasts from both namespaces reach the cross_namespace watcher
        await a.publish_event("orders.created", n=1)
        await b.publish_event("orders.created", n=2)
        await asyncio.sleep(0.3)
        assert sorted(seen) == ["appA.orders.created", "appB.orders.created"]
    finally:
        await asyncio.gather(a.stop(), b.stop(), caller.stop(), watcher.stop())
