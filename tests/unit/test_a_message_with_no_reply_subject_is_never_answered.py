"""A fire-and-forget message is never answered, on any path of the RPC and describe arms.

A message with no reply subject has nobody to answer. `nats.aio.msg.Msg.respond` refuses it, so
what an unguarded `answer` costs in production is a raised `nats.errors.Error` swallowed by the
dispatcher and logged: an error line per fire-and-forget request for a reply nobody asked for, and
on the describe arm a debug line that hides the same mistake. `MockMessage.respond` mirrors the
refusal, and ALSO counts the attempt in `respond_calls` before it refuses, because a refused call
leaves `responded_data` at None -- identical to a call that was never made. So the assertion is on
`respond_calls`, the one counter that tells "not attempted" from "attempted and refused".

Every arm that answers is driven here with a reply subject and again without one:

- the decode failure,
- a refusal from the extension chain,
- a handler that raises,
- the describe success, refusal, and failure arms.

The same scenario WITH a reply subject is the control for each: it must produce exactly one
`respond` call. Without it, "no reply was attempted" would also hold for a scenario that never
reached the arm it names.
"""

from __future__ import annotations

import json

import pytest

import cliffracer.introspect as introspect
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit

JSON = {"Content-Type": "application/json"}


class Refuser(Extension):
    """Refuses every dispatch, RPC and describe alike."""

    fails_closed = True

    async def worker_setup(self, ctx) -> None:
        raise RejectMessage("not for you")


def _service(extension: Extension | None = None) -> CliffracerService:
    class Svc(CliffracerService):
        @rpc
        async def ok(self) -> int:
            return 1

        @rpc
        async def boom(self) -> int:
            raise RuntimeError("handler broke")

    if extension is not None:
        Svc.gate = extension  # type: ignore[attr-defined]
    return Svc(ServiceConfig(name="s", health_listener=False))


async def _drive(case: str, reply: str) -> MockMessage:
    """Send one message of the named kind and return it, with what was done to it."""
    gated = case in ("refused", "describe_refused")
    svc = _service(Refuser() if gated else None)
    await svc.container._setup_extensions()
    svc._discover_handlers()
    dispatcher = svc.container.dispatcher

    if case == "undecodable":
        msg = MockMessage("s.rpc.ok", b"{not json", headers=JSON, reply=reply)
        await dispatcher.handle_rpc_request(msg)
    elif case == "refused":
        msg = MockMessage("s.rpc.ok", b"{}", headers=JSON, reply=reply)
        await dispatcher.handle_rpc_request(msg)
    elif case == "handler_raises":
        msg = MockMessage("s.rpc.boom", b"{}", headers=JSON, reply=reply)
        await dispatcher.handle_rpc_request(msg)
    elif case in ("describe", "describe_refused"):
        msg = MockMessage("s.describe", b"", reply=reply)
        await dispatcher.handle_describe_request(msg)
    else:
        raise AssertionError(case)
    return msg


@pytest.fixture
def describe_that_fails(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("cannot describe")

    monkeypatch.setattr(introspect, "describe", fail)


CASES = [
    "undecodable",
    "refused",
    "handler_raises",
    "describe",
    "describe_refused",
]


@pytest.mark.parametrize("case", CASES)
async def test_a_message_with_no_reply_subject_gets_no_respond_call(case):
    msg = await _drive(case, reply="")

    assert msg.respond_calls == 0, f"{case}: respond was attempted on a message with no reply"


async def test_a_describe_that_fails_is_not_answered_when_there_is_no_reply_subject(
    describe_that_fails,
):
    svc = _service()
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = MockMessage("s.describe", b"", reply="")

    await svc.container.dispatcher.handle_describe_request(msg)

    assert msg.respond_calls == 0


@pytest.mark.parametrize("case", CASES)
async def test_CONTROL_the_same_message_with_a_reply_subject_is_answered_once(case):
    msg = await _drive(case, reply="_INBOX.caller")

    assert msg.respond_calls == 1, f"{case}: the scenario never reached the arm it names"
    assert msg.responded_data is not None


async def test_CONTROL_a_describe_that_fails_is_answered_when_there_is_a_reply_subject(
    describe_that_fails,
):
    svc = _service()
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = MockMessage("s.describe", b"", reply="_INBOX.caller")

    await svc.container.dispatcher.handle_describe_request(msg)

    assert msg.respond_calls == 1
    assert json.loads(msg.responded_data)["code"] == "internal"


async def test_a_message_that_carries_no_reply_attribute_at_all_is_answered():
    """A message with no `reply` attribute is treated as expecting an answer.

    The default is `True`, so `getattr(msg, "reply", True)` on an object that does not define the
    attribute still replies. `MockMessage` always defines `reply`, so the attribute is deleted.
    Its `respond` then refuses (no reply subject), as the real one would, which is why the
    assertion is on the ATTEMPT: `respond_calls` is incremented before the refusal.
    """
    svc = _service()
    await svc.container._setup_extensions()
    svc._discover_handlers()
    dispatcher = svc.container.dispatcher

    rpc_msg = MockMessage("s.rpc.ok", b"{}", headers=JSON)
    del rpc_msg.reply
    describe_msg = MockMessage("s.describe", b"")
    del describe_msg.reply

    await dispatcher.handle_rpc_request(rpc_msg)
    await dispatcher.handle_describe_request(describe_msg)

    assert rpc_msg.respond_calls == 1
    assert describe_msg.respond_calls == 1
