"""A fails_closed extension's own exception does not choose what the wire sees.

`ExtensionPipeline.run_worker` turns any exception out of a `fails_closed`
extension's `worker_setup` into a `RejectMessage`, and the RPC and describe
paths answer a `RejectMessage` reason to the caller verbatim -- they must, or
`refused: unauthenticated` would stop being readable. So the synthesised reason
is the one place that can decide, and it used to embed `str(hook_exc)`.

The exception there is arbitrary: pydantic wraps only `ValueError` and
`AssertionError`, so a user's own `@field_validator` raising anything else
arrives here, as does any bug inside an extension. Whatever it carries -- a
DSN, a path, an upstream body -- went to any caller who could reach the
subject, and `expose_internal_errors` was not consulted in either direction.

The describe path is the second one and is not in the issue: it is answered
with no authentication at all -- the same reach the health endpoint's error
strings had before they were gated.

What must NOT change, and is controlled for below: a deliberate refusal
("unauthenticated", "rate limit exceeded") is authored to be read and is
still delivered whole; the validation path still answers its structured field
errors; and the exception's own text is still written to the log, because
hiding it from the caller is not the same as losing it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from pydantic import BaseModel, field_validator

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension, RejectMessage

pytestmark = pytest.mark.unit

SECRET = "postgres://user:hunter2@10.7.0.5/prod"


class Leaky(Extension):
    """The realistic shape: a check that fails for a reason of its own."""

    fails_closed = True

    async def worker_setup(self, ctx) -> None:
        raise RuntimeError(f"db dsn {SECRET} unreachable")


class Refuser(Extension):
    """CONTROL. An operator-authored reason, which exists to be read."""

    fails_closed = True

    async def worker_setup(self, ctx) -> None:
        raise RejectMessage("unauthenticated")


def _msg(subject: str, data: dict | None = None) -> AsyncMock:
    m = AsyncMock()
    m.subject = subject
    m.data = json.dumps(data or {}).encode()
    m.headers = {}
    m.reply = "_INBOX.test"
    return m


def _replies(msg: AsyncMock) -> list[dict]:
    return [json.loads(c.args[0].decode()) for c in msg.respond.await_args_list]


async def _service(ext: Extension | None, *, expose: bool) -> CliffracerService:
    class Svc(CliffracerService):
        pass

    if ext is not None:
        Svc.probe_ext = ext

    async def handler(self, name: str = "x") -> str:
        return f"hello {name}"

    handler.__name__ = "probe"
    Svc.probe = rpc(handler)

    svc = Svc(ServiceConfig(name="s", expose_internal_errors=expose))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    return svc


async def _rpc_reply(ext: Extension | None, *, expose: bool, payload=None) -> dict:
    svc = await _service(ext, expose=expose)
    msg = _msg("s.rpc.probe", payload)
    await svc.container._handle_rpc_request(msg)
    replies = _replies(msg)
    assert len(replies) == 1, replies
    return replies[0]


async def _describe_reply(ext: Extension, *, expose: bool) -> dict:
    svc = await _service(ext, expose=expose)
    msg = _msg("s.describe")
    await svc.container._handle_describe_request(msg)
    replies = _replies(msg)
    assert len(replies) == 1, replies
    return replies[0]


async def test_a_failed_extension_does_not_put_its_exception_on_the_wire() -> None:
    reply = await _rpc_reply(Leaky(), expose=False)

    body = json.dumps(reply)
    assert SECRET not in body, body
    assert "RuntimeError" not in body, body

    # The caller is still told which extension failed -- withholding the
    # exception is not the same as withholding the outcome -- and is told it as
    # a FAULT. A crashed hook wears `RejectMessage` for control flow, but the
    # caller is not being turned away, and one told "refused" would go looking
    # at its own credentials for a fault that is ours.
    assert reply["success"] is False
    assert reply["error"] == "extension probe_ext failed: internal error"
    assert reply["code"] == "internal", reply
    assert not reply["error"].startswith("refused"), reply


async def test_the_flag_still_opens_it() -> None:
    """The gate is a gate, not a deletion: set the flag and the text is back.

    The type name is what makes this a red before the fix -- the old reason
    interpolated `str(hook_exc)` alone, so `RuntimeError:` was never there,
    exposed or not. Every other gated surface in the process renders an
    exception as `f"{type(exc).__name__}: {exc}"`; this one now does too.
    """
    reply = await _rpc_reply(Leaky(), expose=True)

    assert reply["error"] == (
        f"extension probe_ext failed: RuntimeError: db dsn {SECRET} unreachable"
    )
    assert reply["code"] == "internal", reply


async def test_the_describe_path_reads_the_same_flag() -> None:
    """Describe answers on an unauthenticated subject and used to leak too."""
    hidden = await _describe_reply(Leaky(), expose=False)
    assert SECRET not in json.dumps(hidden), hidden
    assert hidden["error"] == "extension probe_ext failed: internal error"
    assert hidden["code"] == "internal", hidden

    exposed = await _describe_reply(Leaky(), expose=True)
    assert SECRET in exposed["error"], exposed


@pytest.mark.parametrize("expose", [False, True])
async def test_a_deliberate_refusal_is_delivered_whole(expose: bool) -> None:
    """CONTROL. Masking every RejectMessage reason would red this.

    `refused: unauthenticated` is how a caller distinguishes a policy refusal
    from a fault, and `client.py` parses this prefix into `RpcRefusedError`.
    The flag must not touch it in either position.
    """
    reply = await _rpc_reply(Refuser(), expose=expose)

    assert reply["success"] is False
    assert reply["error"] == "refused: unauthenticated"
    assert reply["code"] == "refused", reply


async def test_the_exception_text_is_still_written_to_the_log() -> None:
    """CONTROL. Hidden from the caller, not lost -- the operator still has it."""
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="ERROR")
    try:
        await _rpc_reply(Leaky(), expose=False)
    finally:
        logger.remove(sink)

    assert any(SECRET in m for m in messages), messages


async def test_the_validation_path_still_answers_its_field_errors() -> None:
    """CONTROL. ValidationExtension is the fails_closed extension every service
    binds, and its pydantic failures are structured, not synthesised -- they
    take the `validation_error` branch and must survive untouched.
    """
    reply = await _rpc_reply(None, expose=False, payload={"name": 7})

    assert reply["success"] is False
    assert reply["error"] == "validation failed"
    assert reply["details"], reply


class Order(BaseModel):
    """Module scope because `get_type_hints` resolves a handler's annotations
    against the module, and a class defined inside the test does not resolve.
    """

    sku: str

    @field_validator("sku")
    @classmethod
    def _check(cls, v: str) -> str:
        raise RuntimeError(f"sku lookup against {SECRET} failed")


async def test_a_user_validator_raising_outside_pydantics_wrapping() -> None:
    """The path the issue names: pydantic wraps `ValueError`/`AssertionError`
    and nothing else, so a `@field_validator` raising anything else escapes
    `ValidationExtension` and is synthesised by the pipeline.
    """

    class Svc(CliffracerService):
        pass

    async def handler(self, order: Order) -> str:
        return "ok"

    handler.__name__ = "place"
    Svc.place = rpc(handler)

    svc = Svc(ServiceConfig(name="s", expose_internal_errors=False))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("s.rpc.place", {"order": {"sku": "abc"}})
    await svc.container._handle_rpc_request(msg)

    reply = _replies(msg)[0]
    assert SECRET not in json.dumps(reply), reply
    assert reply["success"] is False
