"""`ServiceTestHarness.describe()` returns a description or raises.

It ended `if isinstance(resp.data, dict): return resp.data; return {}`. No reply at all, which the
dispatcher produces by swallowing a reply failure, came back as `{}`, which reads as a service with
no methods and no events; and a failure envelope, which is a dict, came back as though it were the
description. Each is an error here, saying which.
"""

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit


class Orders(CliffracerService):
    @rpc
    async def place(self, sku: str) -> int:
        return 1


def _harness() -> ServiceTestHarness:
    return ServiceTestHarness(Orders, config=ServiceConfig(name="orders", health_port=0))


@pytest.mark.asyncio
async def test_a_service_that_answers_describe_returns_its_description():
    async with _harness() as harness:
        described = await harness.describe()

    assert described["service"] == "orders"
    assert [m["name"] for m in described["methods"]] == ["place"]


@pytest.mark.asyncio
async def test_no_reply_is_an_error_and_not_an_empty_description(monkeypatch):
    async with _harness() as harness:

        async def silent(msg):
            return None

        monkeypatch.setattr(harness.container.dispatcher, "handle_describe_request", silent)

        with pytest.raises(RuntimeError, match=r"no reply from 'orders'"):
            await harness.describe()


@pytest.mark.asyncio
async def test_a_failure_envelope_is_an_error_and_not_a_description(monkeypatch):
    async with _harness() as harness:

        async def refuse(msg):
            await msg.respond(b'{"success": false, "error": "refused: no", "code": "refused"}')

        monkeypatch.setattr(harness.container.dispatcher, "handle_describe_request", refuse)

        with pytest.raises(RuntimeError, match=r"failure, not a description: 'refused: no'"):
            await harness.describe()


@pytest.mark.asyncio
async def test_a_reply_that_is_not_an_object_is_an_error(monkeypatch):
    async with _harness() as harness:

        async def list_reply(msg):
            await msg.respond(b"[1, 2]")

        monkeypatch.setattr(harness.container.dispatcher, "handle_describe_request", list_reply)

        with pytest.raises(RuntimeError, match=r"not an object"):
            await harness.describe()
