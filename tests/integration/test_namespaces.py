"""Two same-named services in different namespaces don't cross-answer; cross_namespace spans both."""

import asyncio
import json

import nats.errors
import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.core.discovery import HandlerDiscovery

pytestmark = pytest.mark.integration


def _make_user_service(namespace, answered):
    class UserService(CliffracerService):
        @rpc
        async def whoami(self) -> dict[str, str]:
            answered.append(namespace)
            return {"namespace": namespace}

    return UserService(ServiceConfig(name="user_service", namespace=namespace))


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_namespaces_isolate_rpc_and_span_broadcasts():
    answered: list[str] = []
    a = _make_user_service("appA", answered)
    b = _make_user_service("appB", answered)

    # a caller living in appA
    caller = CliffracerService(ServiceConfig(name="caller", namespace="appA"))

    # cross-namespace broadcast listener
    seen = []

    class Watcher(CliffracerService):
        @listener("orders.created", cross_namespace=True, fanout=True)
        async def on_order(self, subject: str, n: int = 0) -> None:
            seen.append(subject)

    watcher = Watcher(ServiceConfig(name="watcher", namespace="watch"))

    # Every start is inside the try, so a start that raises still stops the ones before it:
    # otherwise the leaked-task guard reports its own, misleading error first.
    try:
        await a.start()
        await b.start()
        await caller.start()
        await watcher.start()
        await asyncio.sleep(0.2)

        # RPC stays within appA. Which of two same-named services the broker picks is
        # not isolation, so the test does not read one reply: it makes a run of calls
        # and requires that the appB instance was never invoked, then reads the wire.
        for _ in range(10):
            result = await caller.call_rpc("user_service", "whoami")
            assert result["namespace"] == "appA"
        assert answered == ["appA"] * 10, "an appB instance answered an appA caller"

        def subject_in(namespace):
            return HandlerDiscovery.scoped_subject(
                "user_service.rpc.whoami",
                namespace=namespace,
                subject_prefix=caller.config.subject_prefix,
            )

        # Nothing answers on the un-namespaced subject: neither service subscribes there.
        with pytest.raises(nats.errors.NoRespondersError):
            await caller.nc.request(subject_in(None), b"{}", timeout=2.0)

        # And each namespace's own subject is answered by that namespace's service.
        for namespace in ("appA", "appB"):
            reply = await caller.nc.request(subject_in(namespace), b"{}", timeout=5.0)
            assert json.loads(reply.data)["result"] == {"namespace": namespace}

        # broadcasts from both namespaces reach the cross_namespace watcher
        await a.publish_event("orders.created", n=1)
        await b.publish_event("orders.created", n=2)
        await asyncio.sleep(0.3)
        # The prefix sits OUTSIDE the namespace, so a cross_namespace watcher
        # sees `<prefix>.<namespace>.<pattern>` and still reads only its own
        # environment -- which is the whole reason the prefix is not a namespace.
        assert sorted(seen) == sorted(
            [
                HandlerDiscovery.with_namespace(a.config, "orders.created"),
                HandlerDiscovery.with_namespace(b.config, "orders.created"),
            ]
        )
        assert len({s.split(".")[-3] for s in seen}) == 2, seen
    finally:
        await asyncio.gather(a.stop(), b.stop(), caller.stop(), watcher.stop())
