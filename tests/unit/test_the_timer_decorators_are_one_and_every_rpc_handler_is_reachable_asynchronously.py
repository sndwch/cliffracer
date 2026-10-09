"""The exported `@timer` names its options, and `@rpc` and `@async_rpc` are the same to the framework.

Two `timer` factories existed with near-identical bodies and different signatures: the
exported one folded `headers` and `token_factory` into an undocumented `**kwargs`, though
`token_factory` is how a timer authenticates. The exported one now has the same signature and
builds through the other.

`@async_rpc` sets a marker nothing reads; discovery registers on `_cliffracer_rpc` and the
`async` subject resolves handlers from the same table, so a plain `@rpc` handler is reachable
there too. The docs say so, and the second half pins it.
"""

import inspect
import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, async_rpc, rpc, timer
from cliffracer.core import timer as timer_module

pytestmark = pytest.mark.unit


def test_the_exported_timer_has_the_signature_of_the_one_that_builds_it():
    exported = inspect.signature(timer)
    builder = inspect.signature(timer_module.timer)

    assert list(exported.parameters) == list(builder.parameters)
    assert {"headers", "token_factory"} <= set(exported.parameters)


def test_the_options_reach_the_timer_it_builds():
    def mint() -> str:
        return "t"

    @timer(interval=5, eager=True, headers={"x-tenant": "a"}, token_factory=mint)
    async def tick(self):
        pass

    (built,) = tick._cliffracer_timers
    assert built.token_factory is mint
    assert built.headers == {"x-tenant": "a"}
    assert built.interval == 5 and built.eager is True


def test_CONTROL_an_unknown_option_is_still_an_error_and_bare_use_is_refused():
    with pytest.raises(TypeError):
        timer(interval=1, bogus=1)(lambda self: None)
    with pytest.raises(Exception, match="@timer"):

        @timer
        async def tick(self):
            pass


class Svc(CliffracerService):
    def __init__(self, config):
        super().__init__(config)
        self.ran: list[str] = []

    @rpc
    async def plain(self, data: str) -> None:
        self.ran.append(f"plain:{data}")

    @async_rpc
    async def marked(self, data: str) -> None:
        self.ran.append(f"marked:{data}")


@pytest.mark.parametrize("method", ["plain", "marked"])
async def test_each_handler_is_reachable_on_the_async_subject(method):
    svc = Svc(ServiceConfig(name="asyncs"))
    svc._discover_handlers()
    svc.container.nc = AsyncMock()
    msg = AsyncMock()
    msg.subject = f"asyncs.async.{method}"
    msg.data = json.dumps({"data": "d"}).encode()
    msg.headers = None
    msg.reply = None

    await svc.container.dispatcher.rpc.handle_async_request(msg)
    await svc.container.lifecycle.drain_active_tasks(timeout=2)

    assert svc.ran == [f"{method}:d"]
