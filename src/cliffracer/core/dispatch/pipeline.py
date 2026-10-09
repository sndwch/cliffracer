"""Extension pipeline managing inbound and outbound interceptor chains."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger as global_logger

from ..error_text import exception_text
from ..exceptions import AuthenticationError, AuthorizationError
from ..extension import Extension, RejectMessage, WorkerContext
from ..validation import CONTENT_TYPE_JSON, CONTENT_TYPE_MSGPACK

# The dispatches that have a sender to turn away. A timer firing has none, so a denial there
# stays the error it is and `last_error` names it.
_KINDS_THAT_ANSWER_A_SENDER = frozenset({"rpc", "async_rpc", "event"})


def _copied(value: Any) -> Any:
    """A copy deep enough that a hook cannot reach the caller's containers.

    `dict(payload)` is one level. Every nested container inside it stayed the
    caller's object, shared with the dict the send paths serialise -- and
    `publish_event` puts the caller's own domain dict at `payload["data"]`, so
    `ctx.payload["data"][k] = v` is one dereference from the top-level write
    the contract test already covered. That wrote to the wire AND mutated the
    caller's dict as a side effect of publishing it.

    Containers are rebuilt; anything else is passed through. NOT
    `copy.deepcopy`, for two reasons, and the weaker one is the one that first
    comes to mind. Most values `deepcopy` cannot handle the wire cannot carry
    either -- a lock fails both -- so "deepcopy might raise" is nearly empty.
    Where they DISAGREE is a generator: `deepcopy` raises `cannot pickle`, the
    serialiser renders it `[0, 1, 2]`, so `deepcopy` would refuse a send that
    works today. The reason that does not depend on that case is cost: this is
    about three times cheaper, and it cannot fail on anything.

    THE BOUNDARY, stated because it is a decision: a hook can still mutate the
    ATTRIBUTES of a custom object inside the payload. Copying those would mean
    the same failure mode `deepcopy` has. The contract stops a hook reaching
    the caller's dicts and lists, which is what the send paths serialise and
    what a hook can reach without importing anything.
    """
    if isinstance(value, dict):
        return {k: _copied(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copied(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_copied(v) for v in value)
    if isinstance(value, set):
        return set(value)
    return value


class ExtensionPipeline:
    """Manages interceptor chains across registered extensions.

    Invariants:
    - Forward execution of worker_setup in registered order.
    - If an extension fails closed on setup exception, RejectMessage is raised.
    - Reverse execution of worker_result and worker_teardown in registered order.
    - Forward execution of before_call in registered order.
    - Reverse execution of after_call in registered order.
    - Never raises exceptions from worker_result, worker_teardown, or after_call.
    """

    def __init__(self, extensions: list[Extension], logger: Any = None, config: Any = None) -> None:
        self.extensions = extensions
        self.logger = logger or global_logger
        #: Read only to decide whether a failed extension's exception text may
        #: leave the process. `None` -- a pipeline built without one -- is the
        #: same answer as `expose_internal_errors=False`; see `may_expose`.
        self.config = config

    async def run_worker(self, ctx: WorkerContext, call: Callable[[], Awaitable[Any]]) -> Any:
        """Execute extension worker lifecycle hooks around call."""
        result: Any = None
        exc: BaseException | None = None
        try:
            for ext in self.extensions:
                try:
                    # Called here, not awaited from the argument list, for the
                    # reason `_guarded_hook` gives -- and this is the site where
                    # getting it wrong is worst. A plain `def worker_setup` that
                    # does its work and returns leaves `await None`, which
                    # raises, which on a `fails_closed` extension is converted
                    # to a refusal: a correct hook would refuse EVERY message
                    # with the handler never running.
                    setup_result = ext.worker_setup(ctx)
                    if inspect.isawaitable(setup_result):
                        await setup_result
                except RejectMessage:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as hook_exc:
                    self.logger.error(f"extension {ext.name}.worker_setup raised: {hook_exc}")
                    if getattr(ext, "fails_closed", False):
                        # `hook_crash` is what lets the wire tell this from a
                        # refusal an extension authored. This code is the only
                        # place that knows -- it is inside the `except` arm
                        # around the hook -- so it states the fact rather than
                        # leaving the boundary to reconstruct it.
                        #
                        # The exception is arbitrary: a user's
                        # `@field_validator` raising, or a bug in the extension.
                        # Its text passes the same gate as a handler's, and the
                        # detail is in the log line above either way.
                        raise RejectMessage(
                            f"extension {ext.name} failed: {exception_text(hook_exc, self.config)}",
                            hook_crash=True,
                        ) from hook_exc
            try:
                result = await call()
            except (AuthenticationError, AuthorizationError) as denial:
                if ctx.kind not in _KINDS_THAT_ANSWER_A_SENDER:
                    raise
                # A handler guarded by `@requires_auth` / `@requires_roles` /
                # `@requires_permissions` turned the sender away. That is a refusal,
                # exactly as when the extension refuses before the handler runs, and
                # raised here it reaches every `worker_result` hook as one, so the
                # metrics count a refusal and the wire says `refused`, not that the
                # service broke. The reason is fixed text: the denial's own message
                # names the roles or permissions required, which a refusal's reason
                # would deliver to the caller verbatim. The log keeps that text.
                self.logger.warning(
                    f"{ctx.kind} {ctx.data.get('handler_name') or ctx.subject} denied "
                    f"(correlation_id: {ctx.correlation_id}): {type(denial).__name__}: {denial}"
                )
                reason = (
                    "unauthenticated" if isinstance(denial, AuthenticationError) else "forbidden"
                )
                raise RejectMessage(reason) from denial
            return result
        except BaseException as caught:
            exc = caught
            raise
        finally:
            # THE UNWIND FINISHES EVEN IF THE TASK IS CANCELLED. These were two
            # bare loops, and `_guarded_hook` re-raises `CancelledError` on
            # purpose, so a cancel delivered while any `worker_result` was
            # awaiting ended the unwind where it stood: the remaining
            # `worker_result` hooks were skipped and the whole `worker_teardown`
            # loop never started. Graceful shutdown cancels in-flight dispatch
            # by design, and any extension whose `worker_result` awaits anything
            # opens the window -- so what `worker_setup` acquired leaked on
            # every dispatch that was still running when the service stopped:
            # an unended span that is therefore never exported, a correlation id
            # and an auth context never reset.
            #
            # `docs/extensions.md` promises `worker_teardown` runs "after
            # worker_result, ALWAYS". This is what makes that true.
            #
            # Deferred, NOT swallowed. The first cancellation is re-raised once
            # the unwind is done, so a caller that cancels still gets
            # `CancelledError`; a task that could not be stopped would be a
            # worse failure than a leak. Cancelling again during the unwind does
            # not wedge it either -- each remaining hook's await simply ends
            # early and the walk finishes.
            cancelled: asyncio.CancelledError | None = None
            for hook, args in (("worker_result", (ctx, result, exc)), ("worker_teardown", (ctx,))):
                for ext in reversed(self.extensions):
                    try:
                        await self._guarded_hook(ext, hook, *args)
                    except asyncio.CancelledError as stop:
                        cancelled = cancelled or stop
            if cancelled is not None:
                raise cancelled

    async def run_send_hooks(self, ctx: WorkerContext, send: Callable[[], Awaitable[Any]]) -> Any:
        """Execute extension outbound hooks around send."""
        result: Any = None
        exc: BaseException | None = None
        try:
            for ext in self.extensions:
                await self._guarded_hook(ext, "before_call", ctx)
            result = await send()
            return result
        except BaseException as caught:
            exc = caught
            raise
        finally:
            # The unwind finishes even if the task is cancelled, as `run_worker`'s does, for the
            # reason it gives: `_guarded_hook` re-raises `CancelledError`, so a bare loop ends at the
            # first `after_call` a cancel lands in, and an extension that ends a span or releases a
            # token there never gets to. The first cancellation is raised once every hook has run.
            cancelled: asyncio.CancelledError | None = None
            for ext in reversed(self.extensions):
                try:
                    await self._guarded_hook(ext, "after_call", ctx, result, exc)
                except asyncio.CancelledError as stop:
                    cancelled = cancelled or stop
            if cancelled is not None:
                raise cancelled

    def create_send_context(
        self,
        kind: str,
        subject: str,
        payload: dict[str, Any],
        correlation_id: str,
        serialization_format: str = "json",
    ) -> WorkerContext:
        """Construct initialized outbound WorkerContext."""
        ct = CONTENT_TYPE_MSGPACK if serialization_format == "msgpack" else CONTENT_TYPE_JSON
        return WorkerContext(
            kind=kind,
            subject=subject,
            headers={
                "X-Correlation-ID": correlation_id,
                "correlation_id": correlation_id,
                "Content-Type": ct,
            },
            correlation_id=correlation_id,
            payload=_copied(payload),
        )

    async def run_connection_hook(self, hook: str) -> None:
        """Run `on_disconnect` or `on_reconnect` on every extension, in declaration order.

        Each one is guarded like every other hook: one that raises is logged under its extension's
        name and the rest still run. They run inside the client's own connection callback.
        """
        for ext in self.extensions:
            await self._guarded_hook(ext, hook)

    async def run_listener_hook(
        self, hook: str, subject: str, dependencies: tuple[str, ...]
    ) -> None:
        """Run `on_listener_paused` or `on_listener_resumed` on every extension, in order.

        Guarded as every hook is: one that raises is logged under its extension's name and the
        rest still run.
        """
        for ext in self.extensions:
            await self._guarded_hook(ext, hook, subject, dependencies)

    async def _guarded_hook(self, ext: Extension, hook: str, *args: Any) -> None:
        """Run one extension hook so that nothing it does escapes.

        THE CALL HAPPENS HERE, not in the caller's argument list. It used to
        take an already-built awaitable, which every call site produced as
        `ext.after_call(ctx, result, exc)` -- evaluated in the CALLER, outside
        this try. For an `async def` override that only builds a coroutine and
        raises nothing, so the guard held. For a plain `def` override it runs
        the body, and anything it raised was raised where nothing caught it:
        `worker_result` and `after_call` broke this class's own "never raises"
        invariant, and `before_call` raised before `send()` and stopped the
        message going out at all -- a buggy metrics hook cancelling a send,
        which is the exact failure hook isolation exists to prevent.

        Nothing declares hooks must be coroutine functions and the base class
        being `async def` is not enforcement, so a sync override is a shape
        that reaches here rather than one that cannot.

        A sync hook that behaves is also served: `await`ing its `None` return
        raised `TypeError` inside the guard, so a correct hook was logged as
        `raised: object NoneType can't be used in 'await' expression`. The
        result is awaited only if it is awaitable.
        """
        try:
            result = getattr(ext, hook)(*args)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(f"extension {ext.name}.{hook} raised: {exc}")

    # Compatibility aliases
    _run_worker = run_worker
    _run_send_hooks = run_send_hooks
    _send_context = create_send_context
