"""A hook that CRASHES is a broken service, not a caller being turned away.

`ExtensionPipeline.run_worker` raises `RejectMessage` for both: one an extension
authored ("unauthenticated"), one synthesised because a `fails_closed` hook
raised and the handler must not run. They are the same exception type because
they need the same control flow, and the wire reported them as the same thing --
so an extension bug reached the caller as `RpcRefusedError`.

They route to different people. A refusal says "you are not allowed" and the
caller acts on it; a fault says "this service is broken" and we do. Reporting
the second as the first sends an operator to look at the caller's credentials.

WHAT DECIDES, AND WHAT DELIBERATELY DOES NOT. The pipeline states `hook_crash`
at the raise site, inside the `except` arm around the hook, because that is the
only code that knows. The boundary does NOT infer it from `__cause__`, even
though the synthesised one is raised `from hook_exc` and would be detectable
that way today: `__cause__ is None` is the absence of a declaration, not a
declaration. An extension writing `raise RejectMessage("unauthenticated") from
token_error` is ordinary Python, and inferring would report a genuine refusal as
a service fault -- the mirror of this bug, and worse, because it buries an
authorisation decision in an internal-error bucket. That is pinned below.

THE PREFIX GOES TOO, NOT ONLY THE CODE. A crashed hook's reply no longer begins
`refused: `. That is what makes this work for a client that predates the `code`
field: it classifies on the prefix, finds none that matches, and reaches
`RpcServerError` -- the same class a current client reaches through the code.
Asserted below in both columns, because a fix that only a new client can see
would leave the misattribution standing for every caller mid-deploy.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcRefusedError, RpcServerError
from cliffracer.core.extension import Extension, RejectMessage

pytestmark = pytest.mark.unit

SECRET = "postgres://user:hunter2@10.7.0.5/prod"


class Crasher(Extension):
    """A check that fails for a reason of its own. Nobody authored a refusal."""

    fails_closed = True

    async def worker_setup(self, ctx) -> None:
        raise RuntimeError(f"db dsn {SECRET} unreachable")


class Refuser(Extension):
    """An authored refusal, which must stay one."""

    fails_closed = True

    async def worker_setup(self, ctx) -> None:
        raise RejectMessage("unauthenticated")


class RefuserWithACause(Extension):
    """CONTROL for the design. `from token_error` is ordinary Python.

    This is what makes `__cause__` unusable as the signal: it is set here on a
    refusal nobody would call a fault.
    """

    fails_closed = True

    async def worker_setup(self, ctx) -> None:
        try:
            raise KeyError("x-auth-token")
        except KeyError as token_error:
            raise RejectMessage("unauthenticated") from token_error


def _msg(subject: str) -> AsyncMock:
    m = AsyncMock()
    m.subject = subject
    m.data = json.dumps({}).encode()
    m.headers = {}
    m.reply = "_INBOX.test"
    return m


async def _envelope(ext: Extension, *, expose: bool = False, describe: bool = False) -> dict:
    class Svc(CliffracerService):
        probe_ext = ext

    async def handler(self) -> str:
        return "ok"

    handler.__name__ = "probe"
    Svc.probe = rpc(handler)

    svc = Svc(ServiceConfig(name="s", expose_internal_errors=expose))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _msg("s.describe" if describe else "s.rpc.probe")
    if describe:
        await svc.container._handle_describe_request(msg)
    else:
        await svc.container._handle_rpc_request(msg)
    return json.loads(msg.respond.await_args_list[0].args[0].decode())


async def _classify(envelope: dict, *, without_code: bool = False) -> type[BaseException] | None:
    """The class a client raises for this envelope.

    `without_code` drops the field to stand in for a client that predates it.
    That is faithful because such a client's `_raise_for_error` IS the prefix
    chain this one falls back to when no code is present -- the fallback was
    kept byte-identical when the code path was added above it.
    """
    envelope = {k: v for k, v in envelope.items() if not (without_code and k == "code")}
    client = ServiceClient(service="s", verify=False)
    client._nc = AsyncMock()
    reply = SimpleNamespace(data=json.dumps(envelope).encode(), headers=None)
    with patch.object(ServiceClient, "_request", AsyncMock(return_value=reply)):
        try:
            await client._call("do", {}, int)
        except BaseException as exc:
            return type(exc)
    return None


@pytest.mark.parametrize("describe", [False, True], ids=["rpc", "describe"])
async def test_a_crashed_hook_is_reported_as_a_fault(describe: bool) -> None:
    envelope = await _envelope(Crasher(), describe=describe)

    assert envelope["code"] == "internal", envelope
    assert envelope["error"] == "extension probe_ext failed: internal error", envelope
    assert not envelope["error"].startswith("refused"), envelope
    assert await _classify(envelope) is RpcServerError


@pytest.mark.parametrize("describe", [False, True], ids=["rpc", "describe"])
async def test_an_authored_refusal_stays_a_refusal(describe: bool) -> None:
    """CONTROL. The reason is delivered whole and the class is unchanged."""
    envelope = await _envelope(Refuser(), describe=describe)

    assert envelope["code"] == "refused", envelope
    assert envelope["error"] == "refused: unauthenticated", envelope
    assert await _classify(envelope) is RpcRefusedError


async def test_a_client_that_predates_the_code_field_also_sees_a_fault() -> None:
    """The prefix is what an old client reads, so the prefix has to go too."""
    crash = await _envelope(Crasher())
    refusal = await _envelope(Refuser())

    assert await _classify(crash, without_code=True) is RpcServerError
    assert await _classify(refusal, without_code=True) is RpcRefusedError


async def test_CONTROL_a_refusal_raised_from_another_exception_is_still_a_refusal() -> None:
    """The design pinned: `__cause__` does not decide, and must not.

    This extension raises an authored refusal `from` a `KeyError`, so its
    `__cause__` is set exactly as the synthesised one's is. A boundary reading
    `__cause__` would report this caller's missing token as a service fault.
    """
    envelope = await _envelope(RefuserWithACause())

    assert envelope["code"] == "refused", envelope
    assert envelope["error"] == "refused: unauthenticated", envelope
    assert await _classify(envelope) is RpcRefusedError


async def test_the_exception_text_is_still_governed_by_the_flag() -> None:
    """CONTROL. Reclassifying a crash does not bypass `expose_internal_errors`."""
    hidden = await _envelope(Crasher(), expose=False)
    exposed = await _envelope(Crasher(), expose=True)

    assert SECRET not in json.dumps(hidden), hidden
    assert SECRET in exposed["error"], exposed
    assert hidden["code"] == exposed["code"] == "internal"


async def test_the_flag_alone_does_not_decide_the_class() -> None:
    """CONTROL. A refusal is a refusal with the flag on, and a crash is a crash
    with it off -- otherwise "code is internal" could just mean "text hidden".
    """
    assert (await _envelope(Refuser(), expose=True))["code"] == "refused"
    assert (await _envelope(Crasher(), expose=False))["code"] == "internal"
