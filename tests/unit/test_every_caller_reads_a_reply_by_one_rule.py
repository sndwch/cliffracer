"""A service's `call_rpc`, `cliffracer.calls.call` and a generated client read a reply alike.

A reply that cannot be decoded, that is not an object, that carries no `success` key, or whose
`success` is not true with no `error`, is the remote breaking the protocol: each caller raises
`RpcServerError` naming what came back, never a builtin and never a result. A fake responder on
the in-memory broker answers each body, and each caller calls it.
"""

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from cliffracer import CliffracerService, RpcRefusedError, RpcServerError, ServiceConfig
from cliffracer.calls import call
from cliffracer.client import ServiceClient
from cliffracer.testing import InMemoryBroker, ServiceTestHarness

pytestmark = pytest.mark.unit

JSON = {"Content-Type": "application/json"}


class Caller(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="caller", subject_prefix=None, health_port=0))


Call = Callable[[], Awaitable[Any]]


@contextlib.asynccontextmanager
async def answering(body: bytes, headers: dict[str, str]) -> AsyncIterator[dict[str, Call]]:
    """Each caller, calling `target.m` on a fake that answers `body` with `headers`."""
    broker = InMemoryBroker()
    target, nc = await broker.connect(), await broker.connect()

    async def answer(msg: Any) -> None:
        await target.publish(msg.reply, body, headers=headers)

    await target.subscribe("target.rpc.m", cb=answer)
    service = Caller()
    async with ServiceTestHarness(service, broker=broker):
        client = ServiceClient(nc=nc, service="target", verify=False, subject_prefix="")
        try:
            yield {
                "call_rpc": lambda: service.call_rpc("target", "m"),
                "calls.call": lambda: call(nc, "target", "m", subject_prefix=""),
                "ServiceClient": lambda: client._call("m", {}, Any),
            }
        finally:
            await nc.close()
            await target.close()


MALFORMED = [
    pytest.param(
        b"not json",
        JSON,
        r"target\.rpc\.m answered with something this caller cannot read: "
        r"'not json'$",
        id="undecodable",
    ),
    pytest.param(
        b"\xff\xfe",
        {},
        r"target\.rpc\.m answered with something this caller cannot read",
        id="undecodable-and-unlabelled",
    ),
    pytest.param(
        b"[1, 2]",
        JSON,
        r"target\.rpc\.m answered with list, not an object: '\[1, 2\]'$",
        id="a-list",
    ),
    pytest.param(b"5", JSON, r"answered with int, not an object", id="a-number"),
    pytest.param(b"null", JSON, r"answered with NoneType, not an object", id="null"),
    pytest.param(
        b'{"success": false}',
        JSON,
        r"^target\.rpc\.m failed without saying why: the reply has success=False and no error$",
        id="failed-without-an-error",
    ),
    pytest.param(
        b'{"success": "yes", "result": 1}',
        JSON,
        r"the reply has success='yes' and no error",
        id="success-not-true",
    ),
    pytest.param(
        b'{"result": 5}',
        JSON,
        r"^protocol error: reply from target\.rpc\.m carries no success key$",
        id="no-success-key",
    ),
    pytest.param(b"", JSON, r"carries no success key", id="empty"),
]


@pytest.mark.parametrize(("body", "headers", "says"), MALFORMED)
async def test_a_malformed_reply_is_a_server_error_from_every_caller(body, headers, says):
    async with answering(body, headers) as callers:
        for name, calling in callers.items():
            with pytest.raises(RpcServerError, match=says) as raised:
                await calling()
            assert type(raised.value) is RpcServerError, name


async def test_a_successful_reply_is_its_result_from_every_caller():
    async with answering(b'{"success": true, "result": 5}', JSON) as callers:
        for name, calling in callers.items():
            assert await calling() == 5, name


async def test_an_error_envelope_is_raised_as_its_code_before_success_is_read():
    body = b'{"success": false, "error": "refused: not for you", "code": "refused"}'
    async with answering(body, JSON) as callers:
        for calling in callers.values():
            with pytest.raises(RpcRefusedError, match="not for you"):
                await calling()
