"""A reply that falls back to JSON says so, whatever the request said it was.

`handle_rpc_request` answers in the format the request asked for. When that format cannot be
written (msgpack asked for, the optional extra absent) it answers in JSON instead, and the reply's
Content-Type is what tells the client how to decode it.

The type is recorded by writing `msg.headers["Content-Type"]`, because
`nats.aio.msg.Msg.respond(data)` publishes with `headers=self.headers`: that in-place write IS how
it reaches the wire, and `MockMessage.respond` mirrors it as `response_headers`. So the reply
carries the REQUEST's type unless the dispatcher overwrites it. For every request answered in the
format it asked for, the two are the same string, and a test cannot tell a stamp from an echo
(`harness.rpc()` writes the request's type itself, which is why `resp.headers["Content-Type"]`
there says nothing about the reply). The fallback is the one case where they differ: the request
says msgpack and the bytes are JSON.
"""

import json

import msgpack
import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.dispatch import rpc as rpc_dispatch
from cliffracer.testing import MockMessage, ServiceTestHarness

pytestmark = pytest.mark.unit

JSON = "application/json"
MSGPACK = "application/msgpack"


class Echo(CliffracerService):
    @rpc
    async def echo(self, value: int) -> int:
        return value


async def _ask_in_msgpack(monkeypatch, *, msgpack_writable: bool) -> MockMessage:
    if not msgpack_writable:
        real = rpc_dispatch.serialize_payload

        def serialize(data, format="json"):
            if format == "msgpack":
                raise ImportError("msgpack is not installed")
            return real(data, format=format)

        monkeypatch.setattr(rpc_dispatch, "serialize_payload", serialize)

    config = ServiceConfig(name="echo_svc", health_port=0)
    async with ServiceTestHarness(Echo, config=config) as harness:
        msg = MockMessage(
            "echo_svc.rpc.echo",
            msgpack.packb({"value": 7}),
            headers={"Content-Type": MSGPACK},
        )
        await harness.container.dispatcher.handle_rpc_request(msg)
    return msg


async def test_a_reply_that_falls_back_to_json_is_stamped_as_json(monkeypatch):
    msg = await _ask_in_msgpack(monkeypatch, msgpack_writable=False)

    assert json.loads(msg.responded_data)["result"] == 7, msg.responded_data
    assert msg.response_headers.get("Content-Type") == JSON, msg.response_headers


async def test_CONTROL_the_same_request_is_answered_in_msgpack_when_it_can_be(monkeypatch):
    """Without this, the test above would pass for a dispatcher that always answered in JSON."""
    msg = await _ask_in_msgpack(monkeypatch, msgpack_writable=True)

    assert msgpack.unpackb(msg.responded_data)["result"] == 7, msg.responded_data
    assert msg.response_headers.get("Content-Type") == MSGPACK, msg.response_headers
