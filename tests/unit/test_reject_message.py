"""Tests verifying RejectMessage from worker_setup refuses message dispatch."""

import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension, RejectMessage

pytestmark = pytest.mark.unit


class Rejecter(Extension):
    async def setup(self, ctx):
        self.seen: list[str] = []

    async def worker_setup(self, ctx):
        self.seen.append("setup")
        raise RejectMessage("nope")

    async def worker_result(self, ctx, result, exc):
        self.seen.append(f"result:{type(exc).__name__ if exc else None}")

    async def worker_teardown(self, ctx):
        self.seen.append("teardown")


class Boomer(Extension):
    async def setup(self, ctx):
        self.seen: list[str] = []

    async def worker_setup(self, ctx):
        self.seen.append("setup")
        raise ValueError("a hook bug, not a refusal")

    async def worker_teardown(self, ctx):
        self.seen.append("teardown")


def _msg(subject, data):
    m = AsyncMock()
    m.subject = subject
    m.data = json.dumps(data).encode()
    m.headers = {}
    return m


def _replies(msg):
    return [json.loads(c.args[0].decode()) for c in msg.respond.await_args_list]


async def _dispatch(ext_cls, attr):
    ns = {attr: ext_cls(), "reached": None}

    class Svc(CliffracerService):
        pass

    setattr(Svc, attr, ns[attr])

    async def handler(self) -> str:
        Svc.reached = True
        return "handler ran"

    handler.__name__ = "probe"
    Svc.probe = rpc(handler)
    Svc.reached = False

    svc = Svc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("s.rpc.probe", {})
    await svc.container._handle_rpc_request(msg)
    return svc, msg, Svc.reached


async def test_reject_message_skips_the_handler_and_still_runs_result_and_teardown():
    svc, msg, reached = await _dispatch(Rejecter, "rejecter")
    assert reached is False, "the handler must not run"
    assert svc.rejecter.seen == ["setup", "result:RejectMessage", "teardown"]

    body = json.dumps(_replies(msg)[0])
    assert "nope" in body, body


async def test_a_plain_exception_from_worker_setup_still_does_not():
    """CONTROL. This is the property RejectMessage is the single exception to."""
    svc, msg, reached = await _dispatch(Boomer, "boomer")
    assert reached is True, "a hook bug must not stop the handler"

    body = json.dumps(_replies(msg)[0])
    assert "handler ran" in body, body


class GateBoomer(Extension):
    fails_closed = True

    async def setup(self, ctx):
        self.seen: list[str] = []

    async def worker_setup(self, ctx):
        self.seen.append("setup")
        raise KeyError("missing_auth_header")

    async def worker_teardown(self, ctx):
        self.seen.append("teardown")


async def test_gate_extension_fails_closed_on_unexpected_exception():
    svc, msg, reached = await _dispatch(GateBoomer, "gate_boomer")
    assert reached is False, "handler must not run when gate hook raises"
    body = json.dumps(_replies(msg)[0])
    # The caller is told which gate refused. The KeyError's own text is not the
    # gate's message to anyone -- it passes the same gate as a handler's
    # exception, and this service leaves expose_internal_errors at its default.
    assert "extension gate_boomer failed: internal error" in body, body
    # A crashed hook is reported as a fault, not as a refusal: the caller is not
    # being turned away, the service is broken.
    assert "refused" not in body, body
    assert "missing_auth_header" not in body, body


# --- a RejectMessage from any hook but worker_setup is swallowed like any other exception ------
#
# The guarantee holds only because `_guarded_hook` catches `Exception` generically. Every refusal
# above is raised in `worker_setup`; these raise it where it must change nothing.


@pytest.mark.parametrize("hook", ["worker_result", "worker_teardown"])
async def test_a_refusal_from_a_hook_after_the_handler_does_not_rewrite_its_reply(hook):
    ran: list[str] = []

    async def refuse_too_late(self, *args):
        ran.append(hook)
        raise RejectMessage("too late to refuse")

    late = type("LateRejecter", (Extension,), {hook: refuse_too_late})

    svc, msg, reached = await _dispatch(late, "late")

    assert ran == [hook], "the hook must have run, or this proves nothing"
    assert reached is True
    (reply,) = _replies(msg)
    assert reply["success"] is True and reply["result"] == "handler ran", reply


@pytest.mark.parametrize("hook", ["before_call", "after_call"])
async def test_a_refusal_from_a_send_hook_does_not_stop_the_message_going_out(hook):
    ran: list[str] = []

    async def refuse_the_send(self, *args):
        ran.append(hook)
        raise RejectMessage("not from a send hook")

    refuser = type("SendRejecter", (Extension,), {hook: refuse_the_send})

    class Svc(CliffracerService):
        sender = refuser()

    svc = Svc(ServiceConfig(name="s"))
    svc.nc = AsyncMock()
    await svc.container._setup_extensions()

    await svc.publish_event("orders.created", order_id="o1")

    assert ran == [hook]
    svc.nc.publish.assert_awaited_once()
    assert svc.nc.publish.await_args.args[0] == "orders.created"
