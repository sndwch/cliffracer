"""Extensions see the messages a service sends, not only the ones it consumes.

`worker_setup` / `worker_result` / `worker_teardown` run around consumption.
`before_call` / `after_call` run around the five outbound paths:

    call_rpc          awaited, returns the reply's result
    call_async        fire-and-forget
    call_rpc_no_wait  fire-and-forget
    publish_event     fire-and-forget
    broadcast_message fire-and-forget, plus the websocket fan-out

Each outbound message triggers one before/after pair.

Hooks may modify `ctx.headers` for correlation injection and auth tokens.
`ctx.payload` is read-only by contract, and send paths pass copies to enforce this.

No broker: `service.nc` is a recording double.
"""

import asyncio
import json

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit


class Recorder:
    """A stand-in for `nc` that records what the send paths hand it."""

    def __init__(self):
        self.published: list[tuple[str, bytes, dict]] = []
        self.requested: list[tuple[str, bytes, dict]] = []
        self.reply = {"result": {"ok": True}}

    async def publish(self, subject, data, headers=None, **kw):
        self.published.append((subject, data, dict(headers or {})))

    async def request(self, subject, data, timeout=None, headers=None, **kw):
        self.requested.append((subject, data, dict(headers or {})))

        class _Msg:
            pass

        msg = _Msg()
        msg.data = json.dumps(self.reply).encode()
        return msg


class Spy(Extension):
    """Records every send-side hook call, in the order it happened."""

    def __init__(self, label: str = "spy", header: str | None = None):
        self.label = label
        self.header = header
        self.calls: list[tuple[str, str, str | None]] = []
        self.seen_payloads: list[dict] = []
        self.results: list = []
        self.excs: list = []

    async def before_call(self, ctx):
        self.calls.append(("before", ctx.kind, ctx.subject))
        self.seen_payloads.append(dict(ctx.payload))
        if self.header:
            ctx.headers[self.header] = self.label

    async def after_call(self, ctx, result, exc):
        self.calls.append(("after", ctx.kind, ctx.subject))
        self.results.append(result)
        self.excs.append(exc)


def _service(*extensions):
    """A started-enough service: extensions bound, `nc` recording, no broker."""

    attrs = {f"ext{i}": ext for i, ext in enumerate(extensions)}
    cls = type("Svc", (CliffracerService,), attrs)
    svc = cls(ServiceConfig(name="sender", health_port=0))
    svc.container.nc = Recorder()
    asyncio.get_event_loop().run_until_complete(svc.container._setup_extensions())
    return svc


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


# --- every path runs the pair -----------------------------------------------


@pytest.mark.parametrize(
    "call, kind",
    [
        (lambda s: s.call_rpc("other", "m", x=1), "call_rpc"),
        (lambda s: s.call_async("other", "m", x=1), "call_async"),
        (lambda s: s.call_rpc_no_wait("other", "m", x=1), "call_rpc_no_wait"),
        (lambda s: s.publish_event("things.happened", x=1), "publish_event"),
        (lambda s: s.broadcast_message("things.happened", x=1), "broadcast"),
    ],
)
def test_each_send_path_runs_before_and_after_once(loop, call, kind):
    spy = Spy()
    svc = _service(spy)
    bound_spy = svc.ext0
    loop.run_until_complete(call(svc))
    assert [c[0] for c in bound_spy.calls] == ["before", "after"], bound_spy.calls
    assert {c[1] for c in bound_spy.calls} == {kind}


def test_a_broadcast_fires_one_pair_not_two(loop):
    """The regression the split exists for: `broadcast` must not nest `publish_event`.

    `broadcast_message` reaches the wire through `_publish_event_unhooked`. If
    it called `publish_event` instead, this would record four hook calls and an
    outbound-latency extension would count one message twice.
    """
    spy = Spy()
    svc = _service(spy)
    bound_spy = svc.ext0
    loop.run_until_complete(svc.broadcast_message("things.happened", x=1))
    assert bound_spy.calls == [
        ("before", "broadcast", "things.happened"),
        ("after", "broadcast", "things.happened"),
    ]


# --- what a hook sees --------------------------------------------------------


def test_a_hook_sees_the_namespaced_subject_and_the_real_payload(loop):
    """Asserted against what the RECORDER got, not against what the test passed.

    A hook that saw `things.happened` while the wire saw `proj.things.happened`
    would pass a test written the other way round.
    """
    spy = Spy()
    svc = _service(spy)
    bound_spy = svc.ext0
    svc.config.namespace = "proj"
    loop.run_until_complete(svc.publish_event("things.happened", x=1))

    wire_subject = svc.nc.published[0][0]
    assert bound_spy.calls[0][2] == wire_subject == "proj.things.happened"
    assert bound_spy.seen_payloads[0]["x"] == 1


def test_after_call_receives_the_result(loop):
    spy = Spy()
    svc = _service(spy)
    bound_spy = svc.ext0
    svc.nc.reply = {"result": {"answer": 42}}
    out = loop.run_until_complete(svc.call_rpc("other", "m"))
    assert out == {"answer": 42}
    assert bound_spy.results[-1] == {"answer": 42}
    assert bound_spy.excs[-1] is None


def test_after_call_runs_when_the_send_raises_and_sees_the_exception(loop):
    spy = Spy()
    svc = _service(spy)
    bound_spy = svc.ext0

    async def boom(*a, **k):
        raise RuntimeError("broker gone")

    svc.nc.request = boom
    with pytest.raises(RuntimeError):
        loop.run_until_complete(svc.call_rpc("other", "m"))

    assert [c[0] for c in bound_spy.calls] == ["before", "after"]
    assert isinstance(bound_spy.excs[-1], RuntimeError)


# --- ordering ----------------------------------------------------------------


def test_before_in_declaration_order_after_in_reverse(loop):
    order: list[str] = []

    class Ordered(Extension):
        def __init__(self, label):
            self.label = label

        async def before_call(self, ctx):
            order.append(f"before:{self.label}")

        async def after_call(self, ctx, result, exc):
            order.append(f"after:{self.label}")

    svc = _service(Ordered("a"), Ordered("b"))
    loop.run_until_complete(svc.publish_event("e"))
    assert order == ["before:a", "before:b", "after:b", "after:a"]


# --- headers are the one mutable thing ---------------------------------------


def test_a_header_added_in_before_call_reaches_the_wire(loop):
    """Verify headers modified in before_call are transmitted on the wire."""
    spy = Spy(label="bearer-xyz", header="authorization")
    svc = _service(spy)
    loop.run_until_complete(svc.call_rpc("other", "m"))
    _, _, headers = svc.nc.requested[0]
    assert headers["authorization"] == "bearer-xyz"
    assert "correlation_id" in headers


def test_a_hook_cannot_change_the_payload(loop):
    """The contract is enforced by a copy, not described in a docstring."""

    class Meddler(Extension):
        async def before_call(self, ctx):
            ctx.payload["injected"] = "nope"
            ctx.subject = "somewhere.else"

    svc = _service(Meddler())
    loop.run_until_complete(svc.publish_event("things.happened", x=1))

    subject, data, _ = svc.nc.published[0]
    assert subject == "things.happened"
    assert "injected" not in json.loads(data)


# --- a bad hook cannot break the send ----------------------------------------


@pytest.mark.parametrize("hook", ["before_call", "after_call"])
def test_a_raising_hook_is_swallowed_and_the_call_still_returns(loop, hook):
    """Verify exceptions raised in send-side hooks do not disrupt the RPC call."""

    class Bad(Extension):
        pass

    async def raiser(*a, **k):
        raise RuntimeError("bad hook")

    setattr(Bad, hook, raiser)

    spy = Spy()
    svc = _service(Bad(), spy)
    bound_spy = svc.ext1
    svc.nc.reply = {"result": "fine"}
    out = loop.run_until_complete(svc.call_rpc("other", "m"))

    assert out == "fine"
    # the well-behaved extension still ran both halves
    assert [c[0] for c in bound_spy.calls] == ["before", "after"]
