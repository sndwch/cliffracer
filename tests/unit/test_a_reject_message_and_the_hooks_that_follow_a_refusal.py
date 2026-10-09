"""Two behaviours of the hook chain that the docs now state, pinned here.

1. A `RejectMessage` raised by the HANDLER body is honoured as a refusal, not only one raised
   from `worker_setup`: an RPC caller gets the refusal reply, and an event is not retried or
   dead-lettered (a core event's outcome is `OK`; a JetStream message is acknowledged). A handler
   that has decided a message must not be redelivered says so by raising it. Both
   `worker_result` and `worker_teardown` see it, with the refusal as `exc`.
2. When an extension refuses in `worker_setup`, the extensions declared AFTER it never had their
   `worker_setup` called but still get `worker_result` (with the refusal) and `worker_teardown`.
   An extension that releases in teardown what it acquired in setup has to cope with that.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.core.container import DispatchOutcome
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


def _recording(hooks: list[str]):
    class Recording(Extension):
        async def worker_setup(self, ctx) -> None:
            hooks.append("setup")

        async def worker_result(self, ctx, result, exc) -> None:
            hooks.append(f"result:{type(exc).__name__ if exc else None}")

        async def worker_teardown(self, ctx) -> None:
            hooks.append("teardown")

    return Recording


async def _rpc(svc: CliffracerService, name: str) -> dict:
    msg = AsyncMock()
    msg.subject = f"{svc.config.name}.rpc.{name}"
    msg.data = b"{}"
    msg.headers = {}
    await svc.container._handle_rpc_request(msg)
    return json.loads(msg.respond.await_args_list[0].args[0].decode())


async def test_a_reject_message_from_an_rpc_handler_is_a_refusal_and_the_hooks_see_it():
    hooks: list[str] = []

    class Svc(CliffracerService):
        recorded = _recording(hooks)()

        @rpc
        async def work(self) -> int:
            raise RejectMessage("the handler said no")

    svc = Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    reply = await _rpc(svc, "work")

    assert (reply["success"], reply["error"], reply["code"]) == (
        False,
        "refused: the handler said no",
        "refused",
    )
    assert hooks == ["setup", "result:RejectMessage", "teardown"]


async def test_a_reject_message_from_a_core_event_handler_is_not_an_error():
    hooks: list[str] = []

    class Svc(CliffracerService):
        recorded = _recording(hooks)()

        @listener("e.x", fanout=True)
        async def on_e(self, n: int) -> None:
            raise RejectMessage("the handler refuses this event")

    svc = Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = AsyncMock()
    msg.subject = "e.x"
    msg.data = json.dumps({"n": 1}).encode()
    msg.headers = {}

    outcome = await svc.container.dispatcher.handle_event(msg, raise_on_error=False)

    assert outcome is DispatchOutcome.OK
    assert hooks == ["setup", "result:RejectMessage", "teardown"]


async def test_a_reject_message_from_a_jetstream_handler_acknowledges_the_message():
    class Svc(CliffracerService):
        @listener("events.ping", fanout=True)
        async def on_ping(self, seq: int) -> None:
            raise RejectMessage("not redelivering this one")

    svc = Svc(
        ServiceConfig(
            name="pinger",
            jetstream_enabled=True,
            jetstream_streams=[
                StreamSpec(name="EVENTS", subjects=["events.*"]),
                StreamSpec(name="DLQ", subjects=["dlq.*"]),
            ],
        )
    )
    svc.nc, svc.js = AsyncMock(), AsyncMock()
    svc.container.nc, svc.container.js = svc.nc, svc.js
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = AsyncMock()
    msg.subject, msg.data, msg.headers = "events.ping", b'{"seq": 1}', None
    msg.metadata = SimpleNamespace(num_delivered=1)

    await asyncio.wait_for(
        svc.container._handle_jetstream_event(msg, pattern="events.ping"), timeout=5
    )

    assert msg.ack.await_count == 1
    assert msg.nak.await_count == 0 and msg.term.await_count == 0
    # "Neither retried nor dead-lettered": a dead letter is a publish, on either connection.
    assert svc.js.publish.await_count == 0 and svc.nc.publish.await_count == 0


async def test_the_extensions_after_a_refusing_one_get_result_and_teardown_but_not_setup():
    before: list[str] = []
    after: list[str] = []

    class Refuses(Extension):
        async def worker_setup(self, ctx) -> None:
            raise RejectMessage("unsigned")

    class Svc(CliffracerService):
        first = _recording(before)()
        refuser = Refuses()
        last = _recording(after)()

        @rpc
        async def work(self) -> int:
            return 1

    svc = Svc(ServiceConfig(name="svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    reply = await _rpc(svc, "work")

    assert reply["code"] == "refused"
    assert before == ["setup", "result:RejectMessage", "teardown"]
    assert after == ["result:RejectMessage", "teardown"], "an unpaired teardown is the contract"
