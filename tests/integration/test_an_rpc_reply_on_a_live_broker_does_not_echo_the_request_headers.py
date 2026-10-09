"""A live RPC reply does not hand a caller's request headers back, a bearer token included.

`Msg.respond` publishes the inbound message's own headers, so every reply reused what the caller
sent: an `Authorization: Bearer ...` header went back on the reply, and so did a correlation id the
service had refused. The reply carries its content type and the correlation id the service used.
"""

import json

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.discovery import HandlerDiscovery

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

TOKEN = "Bearer live-token-canary"


class Echoless(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="reply_headers_live", health_port=0))

    @rpc
    async def ok(self, value: int) -> int:
        return value


def _names(reply) -> set[str]:
    return {name.lower() for name in (reply.headers or {})}


@pytest.fixture
async def service():
    svc = Echoless()
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


async def test_a_request_authorization_header_is_not_on_the_reply(nats_connection, service):
    subject = HandlerDiscovery.with_namespace(service.config, "reply_headers_live.rpc.ok")

    reply = await nats_connection.request(
        subject,
        json.dumps({"value": 3}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Correlation-ID": "live-used",
            "Authorization": TOKEN,
            "X-Request-Only": "1",
        },
        timeout=5,
    )

    assert json.loads(reply.data)["result"] == 3
    assert _names(reply) == {"content-type", "x-correlation-id"}, reply.headers
    assert reply.headers["X-Correlation-ID"] == "live-used"
    assert TOKEN not in str(reply.headers)


async def test_a_refusal_reply_does_not_carry_the_request_headers_either(nats_connection, service):
    subject = HandlerDiscovery.with_namespace(service.config, "reply_headers_live.rpc.ok")

    reply = await nats_connection.request(
        subject,
        json.dumps({"value": "not an int"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": TOKEN},
        timeout=5,
    )

    assert json.loads(reply.data)["success"] is False
    assert "authorization" not in _names(reply), reply.headers
    assert "content-type" in _names(reply)


async def test_the_describe_reply_does_not_carry_the_request_headers(nats_connection, service):
    subject = HandlerDiscovery.with_namespace(service.config, "reply_headers_live.describe")

    reply = await nats_connection.request(
        subject, b"{}", headers={"Authorization": TOKEN, "X-Request-Only": "1"}, timeout=5
    )

    assert "authorization" not in _names(reply) and "x-request-only" not in _names(reply)
    assert TOKEN not in str(reply.headers)
