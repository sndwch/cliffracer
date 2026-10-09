"""Extensions see the messages a service sends, not only the ones it consumes.

`worker_setup` / `worker_result` / `worker_teardown` run around consumption.
`before_call` / `after_call` run around the five outbound paths:

    call_rpc          awaited, returns the reply's result
    call_async        fire-and-forget
    call_rpc_no_wait  fire-and-forget
    publish_event     fire-and-forget
    broadcast_message fire-and-forget

Each outbound message triggers one before/after pair.

Hooks may modify `ctx.headers` for correlation injection and auth tokens.
`ctx.payload` is read-only by contract. The send paths copy its containers, so
a write to a dict, list, tuple or set is discarded; a custom object inside it is
passed through, and an attribute set on it is not.

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

#: The five outbound paths, as (how to send one message, the `ctx.kind` its hooks see).
SEND_PATHS = [
    (lambda s: s.call_rpc("other", "m", x=1), "call_rpc"),
    (lambda s: s.call_async("other", "m", x=1), "call_async"),
    (lambda s: s.call_rpc_no_wait("other", "m", x=1), "call_rpc_no_wait"),
    (lambda s: s.publish_event("things.happened", x=1), "publish_event"),
    (lambda s: s.broadcast_message("things.happened", x=1), "broadcast"),
]


@pytest.mark.parametrize("call, kind", SEND_PATHS)
def test_each_send_path_runs_before_and_after_once(loop, call, kind):
    spy = Spy()
    svc = _service(spy)
    bound_spy = svc.ext0
    loop.run_until_complete(call(svc))
    assert [c[0] for c in bound_spy.calls] == ["before", "after"], bound_spy.calls
    assert {c[1] for c in bound_spy.calls} == {kind}


def test_a_broadcast_fires_one_pair_not_two(loop):
    """The regression the split exists for: `broadcast` must not nest `publish_event`.

    `broadcast_message` reaches the wire through `_publish_serialized`. If
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
    assert bound_spy.seen_payloads[0]["data"]["x"] == 1


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


@pytest.mark.parametrize("call, kind", SEND_PATHS, ids=[kind for _, kind in SEND_PATHS])
def test_a_header_added_in_before_call_reaches_the_wire(loop, call, kind):
    """Headers a hook adds are on the wire on EVERY outbound path, not only call_rpc.

    An auth token or a trace context attached in `before_call` is the use the hook exists
    for. Each path builds its headers separately, so each is read from the recorder: a path
    that copied `ctx.headers` before the chain ran would still ship the default headers and
    drop the hook's.
    """
    spy = Spy(label="bearer-xyz", header="authorization")
    svc = _service(spy)
    loop.run_until_complete(call(svc))

    rows = svc.nc.published + svc.nc.requested
    assert len(rows) == 1, (kind, rows)
    headers = rows[0][2]
    assert headers["authorization"] == "bearer-xyz", (kind, headers)
    # the default headers still travel with it
    assert "correlation_id" in headers, (kind, headers)


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


# --- the copy has to reach the nested values too ------------------------------
#
# `test_a_hook_cannot_change_the_payload` above writes a TOP-LEVEL key, which
# is the only case a one-level copy catches. `publish_event` puts the caller's
# own domain dict at `payload["data"]`, so `ctx.payload["data"][k] = v` is one
# dereference away and was the natural shape to write.
#
# Two harms, and the second is the one nothing documents. The wire payload
# changes -- exactly what `extension.py` says must not happen, since a hook
# adding a key surfaces as `extra_forbidden` from a service the caller never
# touched. And the CALLER'S OWN OBJECT is mutated as a side effect of
# publishing it.


class NestedMeddler(Extension):
    """Writes one level down, into a dict nested inside the payload."""

    async def before_call(self, ctx):
        data = ctx.payload.get("data")
        if isinstance(data, dict):
            data["nested_injected"] = "LEAKED"


def test_a_hook_cannot_change_a_nested_value_on_the_wire(loop):
    svc = _service(NestedMeddler())
    caller_owned = {"secret": "orig"}

    loop.run_until_complete(svc.publish_event("things.happened", data=caller_owned))

    _, data, _ = svc.container.nc.published[0]
    assert "nested_injected" not in json.loads(data)["data"], json.loads(data)


def test_a_hook_cannot_mutate_the_callers_own_object(loop):
    """The harm nothing documents: publishing a dict must not change it."""
    svc = _service(NestedMeddler())
    caller_owned = {"secret": "orig"}

    loop.run_until_complete(svc.publish_event("things.happened", data=caller_owned))

    assert caller_owned == {"secret": "orig"}, caller_owned


def test_a_hook_cannot_change_a_nested_value_broadcast(loop):
    """`broadcast_message` is the second path that carries a caller's dict."""
    svc = _service(NestedMeddler())
    caller_owned = {"k": "v"}

    loop.run_until_complete(svc.broadcast_message("things.happened", data=caller_owned))

    _, data, _ = svc.container.nc.published[0]
    assert "nested_injected" not in json.loads(data)["data"], json.loads(data)
    assert caller_owned == {"k": "v"}, caller_owned


def test_CONTROL_a_hook_still_sees_the_nested_value_it_is_meant_to_read(loop):
    """The copy must not hide the payload. A hook that READS a nested value --
    which is what an auditing or routing hook does -- still sees it, and a fix
    that passed an empty or opaque payload would satisfy the tests above."""
    seen = {}

    class Reader(Extension):
        async def before_call(self, ctx):
            seen.update(ctx.payload.get("data", {}))

    svc = _service(Reader())

    loop.run_until_complete(svc.publish_event("things.happened", data={"secret": "orig"}))

    assert seen == {"secret": "orig"}, seen


def test_CONTROL_a_non_dict_nested_value_still_travels(loop):
    """A deep copy must not mangle what it copies: lists, scalars and nested
    lists of dicts all have to arrive as themselves."""
    svc = _service(NestedMeddler())
    payload = {"items": [1, 2, {"a": "b"}], "n": 3, "s": "x"}

    loop.run_until_complete(svc.publish_event("things.happened", **payload))

    sent = json.loads(svc.container.nc.published[0][1])["data"]
    assert sent["items"] == [1, 2, {"a": "b"}], sent
    assert sent["n"] == 3 and sent["s"] == "x", sent


def test_a_payload_the_serialiser_accepts_but_deepcopy_rejects_still_sends(loop):
    """THE REASON THE COPY IS NOT `copy.deepcopy`, narrowed to a case that holds.

    My first version of this used a lock, on the reasoning that `deepcopy`
    raises on one. It does -- and so does the serialiser, so that payload never
    sent either way and the test failed. Most values `deepcopy` cannot handle
    the wire cannot carry, which makes the obvious argument for avoiding it
    mostly empty.

    A GENERATOR is where the two disagree: `deepcopy` raises `cannot pickle
    'generator' object`, and the serialiser turns it into `[0, 1, 2]`. So
    `deepcopy` would refuse a send that works today, which is a regression
    strictly worse than the leak being fixed. The other reason is cost, which
    does not depend on this case at all: the structural copy is about three
    times cheaper.
    """
    svc = _service(NestedMeddler())

    loop.run_until_complete(svc.publish_event("things.happened", seq=(i for i in range(3))))

    assert svc.container.nc.published, "the send did not happen"
    assert json.loads(svc.container.nc.published[0][1])["data"]["seq"] == [0, 1, 2]


class AttributeMeddler(Extension):
    """Sets an attribute on the custom object, wherever the path put it."""

    async def before_call(self, ctx):
        for scope in (ctx.payload, ctx.payload.get("data")):
            if isinstance(scope, dict) and scope.get("order") is not None:
                scope["order"].sku = "CHANGED"


@pytest.mark.parametrize(
    "call, kind",
    [
        (lambda s, o: s.call_rpc("other", "m", order=o), "call_rpc"),
        (lambda s, o: s.call_async("other", "m", order=o), "call_async"),
        (lambda s, o: s.call_rpc_no_wait("other", "m", order=o), "call_rpc_no_wait"),
        (lambda s, o: s.publish_event("things.happened", order=o), "publish_event"),
        (lambda s, o: s.broadcast_message("things.happened", order=o), "broadcast"),
    ],
    ids=["call_rpc", "call_async", "call_rpc_no_wait", "publish_event", "broadcast"],
)
def test_the_copy_does_not_reach_a_custom_objects_attributes(loop, call, kind):
    """The boundary, pinned so it is a decision rather than an oversight.

    Containers are rebuilt; a custom object is passed through, so a hook can
    still set its attributes. Copying those would reintroduce exactly the
    failure `deepcopy` has. The caller's object changes on every path; the
    wire does not change on any, because every path serialises before the hook.
    """

    from pydantic import BaseModel

    class Order(BaseModel):
        sku: str

    svc = _service(AttributeMeddler())
    order = Order(sku="orig")

    loop.run_until_complete(call(svc, order))

    assert order.sku == "CHANGED", "the boundary moved; update the contract, not this test"
    rows = svc.container.nc.published + svc.container.nc.requested
    assert len(rows) == 1, rows
    wire = json.loads(rows[0][1])
    on_wire = (wire.get("order") or wire["data"]["order"])["sku"]
    assert on_wire == "orig", (kind, wire)
