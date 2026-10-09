"""Every send path fixes what it sends before `before_call` runs.

A hook's view of the payload is read-only for the wire: the containers it sees
are copies, and a custom object inside them is the caller's, so setting one of
its attributes changes the caller's object. Whether that change also reached
the message depended on the path. The RPC paths serialised first; `publish_event`
and `broadcast_message` serialised after the hooks, so on those two it did.

Everything that follows from the message is now taken before the hooks too, on
every path:
- the bytes on the wire;
- the idempotency key's payload hash, which must describe those bytes;
- a payload the serialiser refuses, which fails before any hook runs.

No broker: `service.nc` is the recording double from `test_send_side_hooks.py`.
"""

import asyncio
import json

import pytest
from pydantic import BaseModel

from cliffracer.core.idempotency import compute_payload_hash, format_nats_msg_id
from tests.unit.test_send_side_hooks import AttributeMeddler, Spy, _service

pytestmark = pytest.mark.unit


class Order(BaseModel):
    sku: str


PATHS = pytest.mark.parametrize(
    "call",
    [
        lambda s, **kw: s.call_rpc("other", "m", **kw),
        lambda s, **kw: s.call_async("other", "m", **kw),
        lambda s, **kw: s.call_rpc_no_wait("other", "m", **kw),
        lambda s, **kw: s.publish_event("things.happened", **kw),
        lambda s, **kw: s.broadcast_message("things.happened", **kw),
    ],
    ids=["call_rpc", "call_async", "call_rpc_no_wait", "publish_event", "broadcast"],
)


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


@PATHS
def test_a_payload_the_serialiser_refuses_fails_before_any_hook_runs(loop, call):
    spy = Spy()
    svc = _service(spy)

    with pytest.raises(UnicodeDecodeError):
        loop.run_until_complete(call(svc, blob=b"\xff\xfe"))

    assert svc.ext0.calls == [], "a hook ran for a message that was never going to be sent"
    assert svc.container.nc.published == svc.container.nc.requested == []


def test_the_idempotency_key_describes_the_bytes_published(loop):
    """The key is a hash of the payload. A hook that changed the payload after
    the hash was taken sent bytes under a key computed from other bytes, so a
    retry of the same call and this message no longer deduplicate on content."""
    svc = _service(AttributeMeddler())

    loop.run_until_complete(
        svc.publish_event("things.happened", idempotent=True, order=Order(sku="orig"))
    )

    ((subject, data, headers),) = svc.container.nc.published
    sent = json.loads(data)["data"]
    expected = format_nats_msg_id(subject, compute_payload_hash(sent))
    assert headers["Nats-Msg-Id"] == expected, sent


def test_a_broadcast_carries_the_payload_as_it_was_before_the_hooks(loop):
    svc = _service(AttributeMeddler())
    order = Order(sku="orig")

    loop.run_until_complete(svc.broadcast_message("things.happened", order=order))

    assert order.sku == "CHANGED", "the hook did not reach the caller's object"
    ((_, data, _),) = svc.container.nc.published
    assert json.loads(data)["data"] == {"order": {"sku": "orig"}}


def test_a_generator_reaches_the_wire_with_its_values(loop):
    svc = _service()

    loop.run_until_complete(svc.broadcast_message("things.happened", seq=(i for i in range(3))))

    ((_, data, _),) = svc.container.nc.published
    assert json.loads(data)["data"]["seq"] == [0, 1, 2]


def test_CONTROL_a_plain_broadcast_payload_is_published_unchanged(loop):
    svc = _service()

    loop.run_until_complete(svc.broadcast_message("things.happened", x=1, s="y"))

    ((subject, data, _),) = svc.container.nc.published
    assert subject == "things.happened"
    assert json.loads(data)["data"] == {"x": 1, "s": "y"}
