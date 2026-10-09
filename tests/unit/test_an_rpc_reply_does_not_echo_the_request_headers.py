"""An RPC reply carries the headers the service decided on, not the caller's request headers.

`nats.aio.msg.Msg.respond` publishes the inbound message's own headers on the reply, so every reply
reused whatever the caller put on its request, including a correlation id the service had refused
(an ESC byte, an 8 KB value). It went back only to the caller that sent it, so it is not a leak
between callers, but a reply should say what the service used. The reply carries its content type
and the correlation id the service used.
"""

import json

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

UNKNOWN_HEADERS = {
    "X-Secret-Thing": "do-not-echo",
    "X-Request-Only": "1",
    "Authorization": "Bearer t",
}


class Svc(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="svc", subject_prefix=None, health_port=0))

    @rpc
    async def ok(self) -> int:
        return 1

    @rpc
    async def refuses(self) -> int:
        raise RejectMessage("no")

    @rpc
    async def fails(self) -> int:
        raise RuntimeError("boom")

    @rpc
    async def takes(self, n: int) -> int:
        return n


async def started() -> Svc:
    service = Svc()
    await service.container._setup_extensions()
    service._discover_handlers()
    return service


async def call(method: str, headers: dict[str, str], **payload) -> tuple[MockMessage, dict]:
    service = await started()
    msg = MockMessage(
        f"svc.rpc.{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        reply="_INBOX.r",
    )
    await service.container._handle_rpc_request(msg)
    assert msg.responded_data is not None
    return msg, json.loads(msg.responded_data)


def only_what_the_service_decided(msg: MockMessage, body: dict) -> None:
    assert set(msg.response_headers) <= {"Content-Type", "X-Correlation-ID"}, msg.response_headers
    assert msg.response_headers["Content-Type"] == "application/json"
    assert msg.response_headers.get("X-Correlation-ID") == body.get("correlation_id")


@pytest.mark.parametrize("method", ["ok", "refuses", "fails", "takes", "missing"])
async def test_a_header_the_service_does_not_know_is_absent_from_every_kind_of_reply(method):
    msg, body = await call(method, UNKNOWN_HEADERS)

    only_what_the_service_decided(msg, body)
    for name in UNKNOWN_HEADERS:
        assert name not in msg.response_headers


async def test_a_refused_correlation_id_is_not_sent_back():
    bad = "id\x1b[2Jwith-escape"
    msg, body = await call("ok", {"X-Correlation-ID": bad, "correlation_id": bad})

    only_what_the_service_decided(msg, body)
    assert bad not in msg.response_headers.values()
    assert body["correlation_id"] != bad


async def test_an_oversized_correlation_id_is_not_sent_back():
    big = "x" * 8192
    msg, body = await call("ok", {"X-Correlation-ID": big})

    assert big not in msg.response_headers.values()
    only_what_the_service_decided(msg, body)


async def test_CONTROL_the_correlation_id_the_service_used_is_on_the_reply():
    msg, body = await call("ok", {"X-Correlation-ID": "corr_from_the_caller"})

    assert msg.response_headers["X-Correlation-ID"] == "corr_from_the_caller"
    assert body["correlation_id"] == "corr_from_the_caller"


async def test_the_describe_reply_carries_no_request_headers_either():
    service = await started()
    msg = MockMessage("svc.describe", data=b"{}", headers=UNKNOWN_HEADERS, reply="_INBOX.r")

    await service.container._handle_describe_request(msg)

    assert msg.responded_data is not None
    assert set(msg.response_headers) <= {"Content-Type", "X-Correlation-ID"}, msg.response_headers
    for name in UNKNOWN_HEADERS:
        assert name not in msg.response_headers


class RefusesDescribe(Extension):
    async def worker_setup(self, ctx: WorkerContext) -> None:
        if ctx.kind == "describe":
            raise RejectMessage("no introspection")


class DescribeRefused(Svc):
    refuses_describe = RefusesDescribe()


async def test_a_refused_describe_reply_carries_no_request_headers_either():
    service = DescribeRefused()
    await service.container._setup_extensions()
    service._discover_handlers()
    msg = MockMessage("svc.describe", data=b"{}", headers=UNKNOWN_HEADERS, reply="_INBOX.r")

    await service.container._handle_describe_request(msg)

    assert msg.responded_data is not None
    body = json.loads(msg.responded_data)
    assert body["code"] == "refused", body
    only_what_the_service_decided(msg, body)
    for name in UNKNOWN_HEADERS:
        assert name not in msg.response_headers


async def test_a_describe_that_raises_replies_with_no_request_headers_either(monkeypatch):
    def explodes(*args: object, **kwargs: object) -> object:
        raise RuntimeError("describe exploded")

    monkeypatch.setattr("cliffracer.introspect.describe", explodes)
    service = await started()
    msg = MockMessage("svc.describe", data=b"{}", headers=UNKNOWN_HEADERS, reply="_INBOX.r")

    await service.container._handle_describe_request(msg)

    assert msg.responded_data is not None
    body = json.loads(msg.responded_data)
    assert body["code"] == "internal", body
    only_what_the_service_decided(msg, body)
    for name in UNKNOWN_HEADERS:
        assert name not in msg.response_headers


async def test_a_msgpack_request_is_answered_in_msgpack_without_its_other_headers():
    pytest.importorskip("msgpack")
    service = await started()
    import msgpack

    msg = MockMessage(
        "svc.rpc.ok",
        data=msgpack.packb({}),
        headers={"Content-Type": "application/msgpack", "X-Secret-Thing": "x"},
        reply="_INBOX.r",
    )
    await service.container._handle_rpc_request(msg)

    assert msg.response_headers["Content-Type"] == "application/msgpack"
    assert "X-Secret-Thing" not in msg.response_headers
