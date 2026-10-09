"""A subclass of `RejectMessage` is a refusal wherever the base class is.

The decision record says any host or middleware that classifies exceptions tests with
`isinstance`, because the core honours subclasses (`RetryMessage`, and the rate limiter's
`RateLimitExceeded` under it) and a classifier that compared the exact type would read one as a
crash. These drive a subclass of the base class through a real dispatch, raised from a hook and
from a handler.
"""

import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension, RejectMessage

pytestmark = pytest.mark.unit


class QuotaRefused(RejectMessage):
    """A refusal with its own type, which no framework class knows about."""


async def _reply(svc: CliffracerService) -> dict:
    msg = AsyncMock()
    msg.subject = f"{svc.config.name}.rpc.work"
    msg.data = b"{}"
    msg.headers = {}
    await svc.container._handle_rpc_request(msg)
    return json.loads(msg.respond.await_args_list[0].args[0].decode())


async def test_a_subclass_raised_from_worker_setup_is_a_refusal_and_the_handler_does_not_run():
    ran: list[bool] = []

    class Quota(Extension):
        async def worker_setup(self, ctx) -> None:
            raise QuotaRefused("over quota")

    class Svc(CliffracerService):
        quota = Quota()

        @rpc
        async def work(self) -> int:
            ran.append(True)
            return 1

    svc = Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    reply = await _reply(svc)

    assert ran == []
    assert (reply["success"], reply["code"], reply["error"]) == (
        False,
        "refused",
        "refused: over quota",
    )


async def test_a_subclass_raised_by_the_handler_is_a_refusal_too():
    class Svc(CliffracerService):
        @rpc
        async def work(self) -> int:
            raise QuotaRefused("not today")

    svc = Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    reply = await _reply(svc)

    assert (reply["code"], reply["error"]) == ("refused", "refused: not today")


async def test_CONTROL_an_unrelated_exception_is_not_a_refusal():
    class Svc(CliffracerService):
        @rpc
        async def work(self) -> int:
            raise RuntimeError("a crash")

    svc = Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    reply = await _reply(svc)

    assert reply["code"] == "internal"


def test_the_framework_refusals_are_subclasses_of_the_base_class():
    from cliffracer.core.extension import RetryMessage

    assert issubclass(RetryMessage, RejectMessage)
    assert issubclass(QuotaRefused, RejectMessage)
