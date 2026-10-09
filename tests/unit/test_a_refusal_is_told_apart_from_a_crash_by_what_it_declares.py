"""The wire tells a refusal from a crash by the `hook_crash` the exception declares, and no other way.

`RejectMessage` carries `hook_crash`; the RPC and describe arms read it with a default, because a
subclass is free to define its own `__init__` and never set it. An exception that declares nothing
is an AUTHORED refusal: the default must be "not a crash", or a subclass written without calling
`super().__init__` is reported to its caller as the service being broken.

The extras a refusal can add to its reply (`retry_after`, `details`) are for a refusal. A crash is
the service's own fault and its reply carries neither, even when the exception object happens to
hold them: they would tell a caller to retry, or describe a refusal, for a fault that is ours.

Each case has its control: the same exception, declaring the other thing, must give the other
reply, so none of these passes for a dispatcher that always answers one way.
"""

from __future__ import annotations

import json

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit

JSON = {"Content-Type": "application/json"}


class NoSuperInit(RejectMessage):
    """A refusal written the way a subclass may be: it never runs `RejectMessage.__init__`."""

    def __init__(self, reason: str) -> None:
        Exception.__init__(self, reason)
        self.reason = reason


def _raising(make):
    class Gate(Extension):
        async def worker_setup(self, ctx) -> None:
            raise make()

    return Gate()


def _with_extras(*, hook_crash: bool) -> RejectMessage:
    refusal = RejectMessage("slow down", hook_crash=hook_crash)
    refusal.retry_after = 5.0  # type: ignore[attr-defined]
    refusal.details = {"limit": 3}  # type: ignore[attr-defined]
    return refusal


async def _reply(extension: Extension, *, describe: bool = False) -> dict:
    class Svc(CliffracerService):
        @rpc
        async def ok(self) -> int:
            return 1

    Svc.gate = extension  # type: ignore[attr-defined]
    svc = Svc(ServiceConfig(name="s", health_listener=False))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    if describe:
        msg = MockMessage("s.describe", b"")
        await svc.container.dispatcher.handle_describe_request(msg)
    else:
        msg = MockMessage("s.rpc.ok", b"{}", headers=dict(JSON))
        await svc.container.dispatcher.handle_rpc_request(msg)
    assert msg.respond_calls == 1
    return json.loads(msg.responded_data)


@pytest.mark.parametrize("describe", [False, True], ids=["rpc", "describe"])
async def test_a_refusal_that_declares_no_hook_crash_is_reported_as_a_refusal(describe):
    envelope = await _reply(_raising(lambda: NoSuperInit("not for you")), describe=describe)

    assert envelope["code"] == "refused", envelope
    assert envelope["error"] == "refused: not for you", envelope


@pytest.mark.parametrize("describe", [False, True], ids=["rpc", "describe"])
async def test_CONTROL_a_refusal_that_declares_a_crash_is_reported_as_one(describe):
    envelope = await _reply(
        _raising(lambda: RejectMessage("hook blew up", hook_crash=True)), describe=describe
    )

    assert envelope["code"] == "internal", envelope
    assert envelope["error"] == "hook blew up", envelope


async def test_a_crash_reply_carries_neither_a_retry_hint_nor_details():
    envelope = await _reply(_raising(lambda: _with_extras(hook_crash=True)))

    assert envelope["code"] == "internal", envelope
    assert "retry_after" not in envelope, envelope
    assert "details" not in envelope, envelope


async def test_CONTROL_a_refusal_with_the_same_extras_carries_them():
    envelope = await _reply(_raising(lambda: _with_extras(hook_crash=False)))

    assert envelope["code"] == "refused", envelope
    assert envelope["retry_after"] == 5.0, envelope
    assert envelope["details"] == {"limit": 3}, envelope
