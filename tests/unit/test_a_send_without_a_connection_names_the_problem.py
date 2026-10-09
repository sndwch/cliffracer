"""A send on a service that is not connected says so, before any send hook runs.

`call_rpc`, `call_async`, `call_rpc_no_wait`, `publish_event` and `broadcast_message` ended in
`assert self.nc is not None` inside the send. On a service with no connection that was an
`AssertionError` with no message (an `AttributeError` under `python -O`), raised after the
`before_call` hooks had run, so a tracing extension had started a span for a message that could not
leave. The standalone client says `RpcConnectionError` for a connection that is not there; a call
now says the same, a publish says `ServiceLifecycleError`, as a JetStream publish already does when
its context is missing, and both name the service and the subject.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import RpcConnectionError, RpcError, ServiceLifecycleError
from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit


class Recorder(Extension):
    def __init__(self) -> None:
        self.before: list[str] = []
        self.after: list[str] = []

    async def before_call(self, ctx):
        self.before.append(ctx.kind)

    async def after_call(self, ctx, result, exc):
        self.after.append(ctx.kind)


class Svc(CliffracerService):
    recorder = Recorder()


def _service() -> tuple[Svc, Recorder]:
    svc = Svc(ServiceConfig(name="alone", health_port=0))
    (recorder,) = [e for e in svc.extensions if e.name == "recorder"]
    return svc, recorder


CALLS = {
    "call_rpc": lambda s: s.call_rpc("other", "ping"),
    "call_async": lambda s: s.call_async("other", "ping"),
    "call_rpc_no_wait": lambda s: s.call_rpc_no_wait("other", "ping"),
}
#: The subject each call goes to, and the `ctx.kind` each send's hooks see.
SUBJECTS = {
    "call_rpc": "other.rpc.ping",
    "call_async": "other.async.ping",
    "call_rpc_no_wait": "other.rpc.ping",
}
KINDS = {
    "call_rpc": "call_rpc",
    "call_async": "call_async",
    "call_rpc_no_wait": "call_rpc_no_wait",
    "publish_event": "publish_event",
    "broadcast_message": "broadcast",
}
PUBLISHES = {
    "publish_event": lambda s: s.publish_event("orders.created", order_id="o1"),
    "broadcast_message": lambda s: s.broadcast_message("orders.created", order_id="o1"),
}


@pytest.mark.parametrize("send", sorted(CALLS))
async def test_a_call_with_no_connection_raises_rpc_connection_error_naming_both(send):
    svc, _ = _service()

    with pytest.raises(RpcConnectionError) as caught:
        await CALLS[send](svc)

    assert isinstance(caught.value, RpcError)
    message = str(caught.value)
    assert "'alone'" in message and SUBJECTS[send] in message and "not connected" in message


@pytest.mark.parametrize("send", sorted(PUBLISHES))
async def test_a_publish_with_no_connection_raises_service_lifecycle_error_naming_both(send):
    svc, _ = _service()

    with pytest.raises(ServiceLifecycleError) as caught:
        await PUBLISHES[send](svc)

    message = str(caught.value)
    assert "'alone'" in message and "orders.created" in message and "not connected" in message


@pytest.mark.parametrize("send", sorted({**CALLS, **PUBLISHES}))
async def test_no_send_hook_runs_for_a_message_that_cannot_leave(send):
    svc, recorder = _service()

    with pytest.raises((RpcConnectionError, ServiceLifecycleError)):
        await {**CALLS, **PUBLISHES}[send](svc)

    assert (recorder.before, recorder.after) == ([], [])


async def test_CONTROL_an_unusable_subject_is_still_refused_first():
    svc, _ = _service()

    with pytest.raises(ValueError, match="Invalid RPC subject"):
        await svc.call_rpc("other", "")


@pytest.mark.parametrize(
    "send", ["call_async", "call_rpc_no_wait", "publish_event", "broadcast_message"]
)
async def test_CONTROL_a_connected_service_still_sends_through_its_hooks(send):
    svc, recorder = _service()
    svc.nc = AsyncMock()

    await {**CALLS, **PUBLISHES}[send](svc)

    assert svc.nc.publish.await_count == 1
    assert recorder.before == [KINDS[send]] and recorder.after == [KINDS[send]]


async def test_CONTROL_a_connected_service_still_makes_a_call_and_returns_its_result():
    svc, recorder = _service()
    svc.nc = AsyncMock()
    svc.nc.request.return_value = SimpleNamespace(
        data=json.dumps({"success": True, "result": 5}).encode(),
        headers={"Content-Type": "application/json"},
    )

    assert await svc.call_rpc("other", "ping") == 5
    assert recorder.before == ["call_rpc"] and recorder.after == ["call_rpc"]
