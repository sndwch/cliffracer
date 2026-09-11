"""Extension pipeline managing inbound and outbound interceptor chains."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger as global_logger

from ..extension import Extension, RejectMessage, WorkerContext
from ..validation import CONTENT_TYPE_JSON, CONTENT_TYPE_MSGPACK


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

    def __init__(self, extensions: list[Extension], logger: Any = None) -> None:
        self.extensions = extensions
        self.logger = logger or global_logger

    async def run_worker(self, ctx: WorkerContext, call: Callable[[], Awaitable[Any]]) -> Any:
        """Execute extension worker lifecycle hooks around call."""
        result: Any = None
        exc: BaseException | None = None
        try:
            for ext in self.extensions:
                try:
                    await ext.worker_setup(ctx)
                except RejectMessage:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as hook_exc:
                    self.logger.error(f"extension {ext.name}.worker_setup raised: {hook_exc}")
                    if getattr(ext, "fails_closed", False):
                        raise RejectMessage(
                            f"extension {ext.name} failed: {hook_exc}"
                        ) from hook_exc
            result = await call()
            return result
        except BaseException as caught:
            exc = caught
            raise
        finally:
            for ext in reversed(self.extensions):
                await self._guarded_hook(ext, "worker_result", ext.worker_result(ctx, result, exc))
            for ext in reversed(self.extensions):
                await self._guarded_hook(ext, "worker_teardown", ext.worker_teardown(ctx))

    async def run_send_hooks(self, ctx: WorkerContext, send: Callable[[], Awaitable[Any]]) -> Any:
        """Execute extension outbound hooks around send."""
        result: Any = None
        exc: BaseException | None = None
        try:
            for ext in self.extensions:
                await self._guarded_hook(ext, "before_call", ext.before_call(ctx))
            result = await send()
            return result
        except BaseException as caught:
            exc = caught
            raise
        finally:
            for ext in reversed(self.extensions):
                await self._guarded_hook(ext, "after_call", ext.after_call(ctx, result, exc))

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
            payload=dict(payload),
        )

    async def _guarded_hook(self, ext: Extension, hook: str, coro: Awaitable[None]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(f"extension {ext.name}.{hook} raised: {exc}")

    # Compatibility aliases
    _run_worker = run_worker
    _run_send_hooks = run_send_hooks
    _send_context = create_send_context
