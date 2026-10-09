"""A service answers describe from one built description, not one per request."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

import cliffracer.introspect as introspect
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension, RejectMessage

pytestmark = pytest.mark.unit


class Order(BaseModel):
    sku: str


class Receipt(BaseModel):
    ok: bool


class Shop(CliffracerService):
    @rpc
    async def place(self, order: Order) -> Receipt:
        return Receipt(ok=True)


def _msg() -> AsyncMock:
    msg = AsyncMock()
    msg.subject = "shop.describe"
    msg.reply = "reply.1"
    msg.data = b""
    msg.headers = None
    return msg


async def _ask(svc: CliffracerService, count: int = 1) -> list[AsyncMock]:
    messages = [_msg() for _ in range(count)]
    await asyncio.gather(*(svc.container._handle_describe_request(m) for m in messages))
    return messages


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """One entry per call to describe(), which is the work being counted."""
    calls: list[int] = []
    real = introspect.describe

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(introspect, "describe", counting)
    return calls


def _shop(**config) -> Shop:
    svc = Shop(ServiceConfig(name="shop", **config))
    svc._discover_handlers()
    return svc


@pytest.mark.asyncio
async def test_many_describe_requests_build_the_description_once(builds):
    svc = _shop()

    messages = await _ask(svc, count=30)

    assert len(builds) == 1
    bodies = {m.respond.call_args.args[0] for m in messages}
    assert len(bodies) == 1
    answered = json.loads(bodies.pop())
    assert [m["name"] for m in answered["methods"]] == ["place"]


@pytest.mark.asyncio
async def test_the_served_bytes_are_what_a_fresh_description_would_say(builds):
    svc = _shop()
    await _ask(svc)  # fills whatever is cached

    (served,) = await _ask(svc)

    fresh = introspect.canonical(
        introspect.describe(
            Shop, service=svc.config.name, version=svc.config.version, config=svc.config
        ).to_dict()
    )
    assert served.respond.call_args.args[0] == fresh.encode()


@pytest.mark.asyncio
async def test_a_config_assigned_after_the_first_request_is_described_afresh(builds):
    svc = _shop()
    (before,) = await _ask(svc)

    svc.config.version = "9.9.9"
    (after,) = await _ask(svc)

    assert json.loads(before.respond.call_args.args[0])["version"] != "9.9.9"
    assert json.loads(after.respond.call_args.args[0])["version"] == "9.9.9"
    assert len(builds) == 2


@pytest.mark.asyncio
async def test_a_refusal_applies_to_every_request_not_only_the_first(builds):
    class Gate(Extension):
        allowed = 1

        async def worker_setup(self, ctx):
            if ctx.kind == "describe":
                if self.allowed <= 0:
                    raise RejectMessage("no token")
                self.allowed -= 1

    class Guarded(CliffracerService):
        gate = Gate()

        @rpc
        async def place(self, order: Order) -> Receipt:
            return Receipt(ok=True)

    svc = Guarded(ServiceConfig(name="shop"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    (first,) = await _ask(svc)
    (second,) = await _ask(svc)

    assert "methods" in json.loads(first.respond.call_args.args[0])
    assert json.loads(second.respond.call_args.args[0])["error"] == "refused: no token"
