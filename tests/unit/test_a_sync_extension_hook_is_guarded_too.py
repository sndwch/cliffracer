"""A hook that is a plain `def` is isolated like every other hook.

`_guarded_hook` used to take an already-built awaitable, and every call site
built it in the argument expression -- `ext.after_call(ctx, result, exc)` --
which Python evaluates in the CALLER, outside the guard's `try`. For an
`async def` override that only constructs a coroutine and raises nothing, so the
guard held and the suite was green. For a plain `def` override it runs the body,
and whatever it raised was raised where nothing caught it.

Three contracts said that cannot happen:

- `ExtensionPipeline`'s own invariant: "Never raises exceptions from
  worker_result, worker_teardown, or after_call."
- `RejectMessage`'s docstring: every other hook exception "is logged under its
  extension's name and cannot change the handler's outcome".
- `docs/extensions.md`: "a hook that raises is logged and changes nothing", and
  "no hook can cancel a send".

`before_call` broke the last one outright: it raised before `await send()`, so
the message never went out and the exception reached the application code that
called `publish_event`. A buggy metrics extension could stop a service sending
anything, which is the exact failure hook isolation exists to prevent.

WHY A SYNC HOOK IS A REAL SHAPE. Nothing validates that an extension's hooks are
coroutine functions -- `iscoroutinefunction` appears in this tree only for
HANDLERS -- and a base class declaring them `async def` is not enforcement. An
extension is ordinary user code in another repository; `def` instead of
`async def` is a typo away, and the existing send-side test installs
`async def raiser`, so the suite only ever asked the easy question.

The second defect, which the issue did not name: a sync hook that behaves
CORRECTLY was reported as a crash. `await`ing its `None` return raised
`TypeError` inside the guard, so the log said `raised: object NoneType can't be
used in 'await' expression` for a hook that did its job and returned.
"""

import pytest
from loguru import logger

from cliffracer.core.dispatch.pipeline import ExtensionPipeline
from cliffracer.core.extension import Extension, RejectMessage

pytestmark = pytest.mark.unit


async def _ok():
    return "handler-result"


def _pipeline(ext):
    return ExtensionPipeline([ext], logger, None)


# --- a sync hook that raises is contained ------------------------------------


class SyncWorkerResult(Extension):
    name = "sync_worker_result"

    def worker_result(self, ctx, result, exc):
        raise RuntimeError("boom from worker_result")


class SyncWorkerTeardown(Extension):
    name = "sync_worker_teardown"

    def worker_teardown(self, ctx):
        raise RuntimeError("boom from worker_teardown")


class SyncAfterCall(Extension):
    name = "sync_after_call"

    def after_call(self, ctx, result, exc):
        raise RuntimeError("boom from after_call")


class SyncBeforeCall(Extension):
    name = "sync_before_call"

    def before_call(self, ctx):
        raise RuntimeError("boom from before_call")


@pytest.mark.parametrize(
    "ext", [SyncWorkerResult(), SyncWorkerTeardown()], ids=["result", "teardown"]
)
async def test_a_sync_worker_hook_that_raises_does_not_reach_the_caller(ext):
    """The invariant this class states about itself, for a `def` override."""
    pipeline = _pipeline(ext)
    ctx = pipeline.create_send_context("event", "s", {}, "cid")

    assert await pipeline.run_worker(ctx, _ok) == "handler-result"


async def test_a_sync_after_call_that_raises_does_not_replace_the_result():
    pipeline = _pipeline(SyncAfterCall())
    ctx = pipeline.create_send_context("event", "s", {}, "cid")
    sent = []

    result = await pipeline.run_send_hooks(ctx, lambda: _send(sent))

    assert result == "sent"
    assert sent == [1]


async def test_a_sync_before_call_that_raises_does_not_cancel_the_send():
    """The worst of the three: the message never went out.

    `before_call` is evaluated before `await send()`, so the exception arrived
    ahead of the send and propagated to whoever called `publish_event`.
    """
    pipeline = _pipeline(SyncBeforeCall())
    ctx = pipeline.create_send_context("event", "s", {}, "cid")
    sent = []

    result = await pipeline.run_send_hooks(ctx, lambda: _send(sent))

    assert sent == [1], "a hook cannot cancel a send"
    assert result == "sent"


async def _send(sent):
    sent.append(1)
    return "sent"


# --- a sync hook that behaves is not reported as a crash ---------------------


class SyncWellBehaved(Extension):
    name = "sync_ok"

    def __init__(self):
        self.seen = []

    def worker_result(self, ctx, result, exc):
        self.seen.append(result)


async def test_a_sync_hook_that_returns_normally_is_not_logged_as_an_error(caplog):
    """`await None` raised inside the guard, so a correct hook read as a crash."""
    import logging

    ext = SyncWellBehaved()
    handler_id = logger.add(caplog.handler, level="ERROR", format="{message}")
    try:
        pipeline = _pipeline(ext)
        ctx = pipeline.create_send_context("event", "s", {}, "cid")
        with caplog.at_level(logging.ERROR):
            await pipeline.run_worker(ctx, _ok)
    finally:
        logger.remove(handler_id)

    assert ext.seen == ["handler-result"], "the hook must still run and see the result"
    assert "await" not in caplog.text, caplog.text
    assert caplog.text.strip() == "", f"a hook that returned normally was logged: {caplog.text}"


# --- controls ----------------------------------------------------------------


class AsyncRaises(Extension):
    name = "async_raiser"

    async def worker_result(self, ctx, result, exc):
        raise RuntimeError("boom from an async hook")


class AsyncWellBehaved(Extension):
    name = "async_ok"

    def __init__(self):
        self.calls = []

    async def worker_result(self, ctx, result, exc):
        self.calls.append("worker_result")

    async def worker_teardown(self, ctx):
        self.calls.append("worker_teardown")


async def test_CONTROL_an_async_hook_that_raises_is_still_contained():
    """The case that already worked, which this must not break.

    If only the sync tests above were here, replacing the guard with a bare
    `pass` would pass every one of them.
    """
    pipeline = _pipeline(AsyncRaises())
    ctx = pipeline.create_send_context("event", "s", {}, "cid")

    assert await pipeline.run_worker(ctx, _ok) == "handler-result"


async def test_CONTROL_an_async_hook_is_still_awaited():
    """Not merely called. A coroutine that is created and dropped runs nothing,
    and every assertion above about containment would hold for a guard that
    never awaited anything at all."""
    ext = AsyncWellBehaved()
    pipeline = _pipeline(ext)
    ctx = pipeline.create_send_context("event", "s", {}, "cid")

    await pipeline.run_worker(ctx, _ok)

    assert ext.calls == ["worker_result", "worker_teardown"], ext.calls


# --- worker_setup, where getting this wrong is worst -------------------------
#
# `worker_setup` does not go through `_guarded_hook`: it has its own `try`,
# because it is the one hook whose `RejectMessage` must propagate. That try had
# the same shape as the guard's -- `await ext.worker_setup(ctx)` -- so a plain
# `def` override reached it the same way.
#
# The consequence is worse here than anywhere else, and it lands on a CORRECT
# hook. A sync `worker_setup` that does its work and returns leaves `await
# None`, which raises `TypeError`, which on a `fails_closed` extension the
# pipeline converts to `RejectMessage(hook_crash=True)`. So the extension
# refuses EVERY message and no handler ever runs -- a service that answers
# nothing, from a hook that did its job.


class SyncSetupOk(Extension):
    """A correct plain-`def` worker_setup: does its work, returns nothing."""

    def __init__(self, *, fails_closed: bool):
        self.fails_closed = fails_closed
        self.ran: list[int] = []

    @property
    def name(self) -> str:
        return "sync_setup"

    def worker_setup(self, ctx):
        self.ran.append(1)


class SyncSetupRefuses(Extension):
    """The refusal channel, from a sync hook."""

    name = "sync_refuser"
    fails_closed = True

    def worker_setup(self, ctx):
        raise RejectMessage("unauthenticated")


async def test_a_correct_sync_worker_setup_does_not_refuse_every_message():
    """The headline: `fails_closed` turned a working hook into a total outage."""
    ext = SyncSetupOk(fails_closed=True)
    pipeline = _pipeline(ext)
    ctx = pipeline.create_send_context("event", "s", {}, "cid")

    assert await pipeline.run_worker(ctx, _ok) == "handler-result"
    assert ext.ran == [1], "the hook must have run"


async def test_a_correct_sync_worker_setup_is_fine_without_fails_closed_too():
    ext = SyncSetupOk(fails_closed=False)
    pipeline = _pipeline(ext)
    ctx = pipeline.create_send_context("event", "s", {}, "cid")

    assert await pipeline.run_worker(ctx, _ok) == "handler-result"
    assert ext.ran == [1]


async def test_CONTROL_a_sync_worker_setup_can_still_refuse():
    """`RejectMessage` is the one hook exception that changes the outcome, and
    it must keep working from a `def` override -- classified as a POLICY
    refusal, not as the crash that `await None` used to manufacture."""
    pipeline = _pipeline(SyncSetupRefuses())
    ctx = pipeline.create_send_context("event", "s", {}, "cid")
    ran = []

    async def call():
        ran.append(1)
        return "handler-result"

    with pytest.raises(RejectMessage) as exc:
        await pipeline.run_worker(ctx, call)

    assert exc.value.hook_crash is False, "a refusal an extension authored is not a crash"
    assert ran == [], "a refused message must not reach the handler"
