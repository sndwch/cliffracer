"""Synchronous, asynchronous, and introspection RPC dispatch pipeline."""

from __future__ import annotations

import asyncio
import inspect
import json
import traceback
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any

from loguru import logger as global_logger

from ..correlation import CorrelationContext
from ..extension import RejectMessage, WorkerContext
from ..registry import ServiceRegistry
from ..service_config import ServiceConfig
from ..validation import (
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_MSGPACK,
    deserialize_payload,
    serialize_payload,
)
from .pipeline import ExtensionPipeline


class RpcDispatcher:
    """Inbound synchronous RPC, fire-and-forget async RPC, and describe handler.

    Invariants:
    - Bounded concurrency enforced via asyncio.Semaphore if configured.
    - RPC errors formatted into structured JSON envelopes with timestamp and correlation_id.
    - Inbound requests executed through ExtensionPipeline.run_worker.
    """

    def __init__(
        self,
        registry: ServiceRegistry,
        config: ServiceConfig,
        pipeline: ExtensionPipeline,
        task_spawner: Callable[[Coroutine[Any, Any, Any], str | None], asyncio.Task[Any]]
        | None = None,
        logger: Any = None,
        service: Any = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.pipeline = pipeline
        self.task_spawner = task_spawner
        self.logger = logger or global_logger.bind(service=config.name)
        self.service = service

        self._rpc_semaphore: asyncio.Semaphore | None = None
        self._async_rpc_semaphore: asyncio.Semaphore | None = None

    def _spawn_task(
        self, coro: Coroutine[Any, Any, Any], name: str | None = None
    ) -> asyncio.Task[Any]:
        if self.task_spawner is not None:
            return self.task_spawner(coro, name)
        return asyncio.create_task(coro, name=name)

    def get_rpc_semaphore(self) -> asyncio.Semaphore | None:
        """Get or initialize the RPC concurrency semaphore."""
        limit = self.config.max_rpc_concurrency
        if self._rpc_semaphore is None and limit is not None and limit > 0:
            self._rpc_semaphore = asyncio.Semaphore(limit)
        return self._rpc_semaphore

    def get_async_rpc_semaphore(self) -> asyncio.Semaphore | None:
        """Get or initialize the async RPC concurrency semaphore."""
        limit = self.config.max_async_rpc_concurrency
        if limit is None:
            limit = self.config.max_rpc_concurrency
        if self._async_rpc_semaphore is None and limit is not None and limit > 0:
            self._async_rpc_semaphore = asyncio.Semaphore(limit)
        return self._async_rpc_semaphore

    async def on_rpc_request(self, msg: Any) -> None:
        """Handle incoming NATS RPC subscription message."""
        sem = self.get_rpc_semaphore()
        if sem is not None:
            await sem.acquire()
            self._spawn_task(
                self._bounded_handle_rpc(msg, sem),
                name="rpc_bounded_request",
            )
        else:
            self._spawn_task(
                self.handle_rpc_request(msg),
                name="rpc_request",
            )

    async def _bounded_handle_rpc(self, msg: Any, sem: asyncio.Semaphore) -> None:
        try:
            await self.handle_rpc_request(msg)
        except Exception as e:
            self.logger.debug(f"RPC request failed: {e}")
        finally:
            sem.release()

    async def on_describe_request(self, msg: Any) -> None:
        """Handle incoming NATS describe subscription message."""
        self._spawn_task(
            self.handle_describe_request(msg),
            name="describe_request",
        )

    async def on_async_request(self, msg: Any) -> None:
        """Handle incoming NATS fire-and-forget async RPC subscription message."""
        sem = self.get_async_rpc_semaphore()
        if sem is not None:
            await sem.acquire()
            self._spawn_task(
                self._bounded_handle_async_rpc(msg, sem),
                name="async_rpc_bounded_request",
            )
        else:
            self._spawn_task(
                self.handle_async_request(msg),
                name="async_rpc_request",
            )

    async def _bounded_handle_async_rpc(self, msg: Any, sem: asyncio.Semaphore) -> None:
        try:
            await self.handle_async_request(msg)
        finally:
            sem.release()

    async def _run_worker(self, ctx: WorkerContext, call: Callable[[], Awaitable[Any]]) -> Any:
        return await self.pipeline.run_worker(ctx, call)

    async def handle_rpc_request(self, msg: Any) -> None:
        """Execute RPC dispatch pipeline, validating input and replying with an envelope."""
        subject = msg.subject
        handler_name = subject.split(".")[-1]
        has_reply = bool(getattr(msg, "reply", True))

        msg_h = getattr(msg, "headers", None)
        headers = dict(msg_h) if isinstance(msg_h, Mapping) else {}
        content_type = None
        for k, v in headers.items():
            if k.lower() == "content-type":
                content_type = v.split(";")[0].strip().lower()
                break

        reply_format = self.config.serialization_format
        if content_type == CONTENT_TYPE_MSGPACK:
            reply_format = "msgpack"
        elif content_type == CONTENT_TYPE_JSON:
            reply_format = "json"
        elif not content_type:
            if msg.data and msg.data.lstrip().startswith((b"{", b"[")):
                reply_format = "json"

        async def _respond(resp_data: dict[str, Any]) -> None:
            try:
                resp_bytes, resp_ct = serialize_payload(resp_data, format=reply_format)
                if hasattr(msg, "headers"):
                    if msg.headers is None:
                        msg.headers = {}
                    msg.headers["Content-Type"] = resp_ct
                await msg.respond(resp_bytes)
            except Exception as e:
                self.logger.debug(f"Failed to send RPC reply: {e}")

        if handler_name not in self.registry.rpc_handlers:
            if has_reply:
                cid = CorrelationContext.extract_from_headers(headers)
                error_response: dict[str, Any] = {
                    "success": False,
                    "error": f"Unknown method: {handler_name}",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "correlation_id": cid,
                }
                await _respond(error_response)
            return

        handler = self.registry.rpc_handlers[handler_name]
        spec = self.registry.rpc_specs[handler_name]

        try:
            data = deserialize_payload(
                msg.data,
                content_type=content_type,
                fallback_format=self.config.serialization_format,
            )
        except Exception as e:
            self.logger.error(f"Error decoding payload for RPC request {handler_name}: {e}")
            if has_reply:
                cid = CorrelationContext.extract_from_headers(headers)
                error_response = {
                    "success": False,
                    "error": "validation failed",
                    "details": [
                        {
                            "loc": ["__root__"],
                            "msg": f"Invalid payload: {e}",
                            "type": "payload_invalid",
                        }
                    ],
                    "timestamp": datetime.now(UTC).isoformat(),
                    "correlation_id": cid,
                }
                await _respond(error_response)
            return

        ctx = WorkerContext(
            kind="rpc",
            subject=subject,
            headers=headers,
            correlation_id=None,
            payload=data,
            raw=msg,
        )
        ctx.data["handler_name"] = handler_name

        async def call() -> Any:
            self.logger.info(
                f"RPC request {handler_name} with correlation_id: {ctx.correlation_id}"
            )
            raw_kwargs = ctx.data.get("validated_kwargs")
            if raw_kwargs is None:
                raw_kwargs = ctx.payload if isinstance(ctx.payload, dict) else {}
            kwargs = dict(raw_kwargs)
            if spec.takes_correlation_id:
                kwargs["correlation_id"] = ctx.correlation_id
            if inspect.iscoroutinefunction(handler):
                result = await handler(**kwargs)
            else:
                result = handler(**kwargs)
            return spec.return_adapter.dump_python(
                spec.return_adapter.validate_python(result), mode="json"
            )

        try:
            result = await self._run_worker(ctx, call)
        except RejectMessage as e:
            if has_reply:
                error = ctx.data.get("validation_error")
                if error is not None:
                    response: dict[str, Any] = {
                        "success": False,
                        "error": "validation failed",
                        "details": json.loads(error.json()),
                        "timestamp": datetime.now(UTC).isoformat(),
                        "correlation_id": ctx.correlation_id,
                    }
                else:
                    response = {
                        "success": False,
                        "error": f"refused: {e}",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "correlation_id": ctx.correlation_id,
                    }
                await _respond(response)
            return
        except Exception as e:
            self.logger.exception(
                f"Error handling RPC request {handler_name} "
                f"(correlation_id: {ctx.correlation_id}): {e}"
            )
            if has_reply:
                if getattr(self.config, "expose_internal_errors", False):
                    error_response = {
                        "success": False,
                        "error": str(e) or e.__class__.__name__,
                        "traceback": traceback.format_exc(),
                        "timestamp": datetime.now(UTC).isoformat(),
                        "correlation_id": ctx.correlation_id,
                    }
                else:
                    error_response = {
                        "success": False,
                        "error": f"Internal server error (correlation_id: {ctx.correlation_id})",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "correlation_id": ctx.correlation_id,
                    }
                await _respond(error_response)
            return

        if has_reply:
            response = {
                "success": True,
                "result": result,
                "timestamp": datetime.now(UTC).isoformat(),
                "correlation_id": ctx.correlation_id,
            }
            await _respond(response)

    async def handle_describe_request(self, msg: Any) -> None:
        """Answer this service's Description metadata in canonical bytes."""
        from cliffracer.introspect import canonical, describe

        headers = dict(msg.headers) if getattr(msg, "headers", None) else {}
        ctx = WorkerContext(
            kind="describe",
            subject=msg.subject,
            headers=headers,
            correlation_id=None,
            payload={},
            raw=msg,
        )

        target_service = self.service or self

        async def call() -> Any:
            return canonical(
                describe(
                    type(target_service),
                    service=self.config.name,
                    version=self.config.version,
                    config=self.config,
                ).to_dict()
            )

        has_reply = bool(getattr(msg, "reply", True))
        try:
            body = await self._run_worker(ctx, call)
            if has_reply:
                try:
                    await msg.respond(body.encode())
                except Exception as e:
                    self.logger.debug(f"Failed to send describe reply: {e}")
        except RejectMessage as e:
            if has_reply:
                try:
                    await msg.respond(
                        json.dumps(
                            {
                                "success": False,
                                "error": f"refused: {e}",
                                "timestamp": datetime.now(UTC).isoformat(),
                                "correlation_id": ctx.correlation_id,
                            }
                        ).encode()
                    )
                except Exception as reply_err:
                    self.logger.debug(f"Failed to send describe refusal reply: {reply_err}")
        except Exception as e:
            self.logger.error(f"Error answering describe for {self.config.name}: {e}")
            if has_reply:
                try:
                    await msg.respond(
                        json.dumps(
                            {
                                "success": False,
                                "error": str(e) or e.__class__.__name__,
                                "timestamp": datetime.now(UTC).isoformat(),
                                "correlation_id": ctx.correlation_id,
                            }
                        ).encode()
                    )
                except Exception as reply_err:
                    self.logger.debug(f"Failed to send describe error reply: {reply_err}")

    async def handle_async_request(self, msg: Any) -> None:
        """Handle incoming fire-and-forget async RPC requests."""
        subject = msg.subject
        handler_name = subject.split(".")[-1]

        if handler_name not in self.registry.rpc_handlers:
            self.logger.warning(f"Unknown async method: {handler_name}")
            return

        handler = self.registry.rpc_handlers[handler_name]
        spec = self.registry.rpc_specs[handler_name]

        msg_h = getattr(msg, "headers", None)
        headers = dict(msg_h) if isinstance(msg_h, Mapping) else {}
        content_type = None
        for k, v in headers.items():
            if k.lower() == "content-type":
                content_type = v.split(";")[0].strip().lower()
                break

        try:
            data = deserialize_payload(
                msg.data,
                content_type=content_type,
                fallback_format=self.config.serialization_format,
            )
        except Exception as e:
            self.logger.error(f"Error decoding payload for async request {handler_name}: {e}")
            return

        ctx = WorkerContext(
            kind="async_rpc",
            subject=subject,
            headers=headers,
            correlation_id=None,
            payload=data,
            raw=msg,
        )
        ctx.data["handler_name"] = handler_name

        async def call() -> Any:
            raw_kwargs = ctx.data.get("validated_kwargs")
            if raw_kwargs is None:
                raw_kwargs = ctx.payload if isinstance(ctx.payload, dict) else {}
            kwargs = dict(raw_kwargs)
            if spec.takes_correlation_id:
                kwargs["correlation_id"] = ctx.correlation_id
            self.logger.info(
                f"Async request {handler_name} with correlation_id: {ctx.correlation_id}"
            )
            if inspect.iscoroutinefunction(handler):
                return await handler(**kwargs)
            return handler(**kwargs)

        try:
            await self._run_worker(ctx, call)
        except RejectMessage as e:
            error = ctx.data.get("validation_error")
            if error is not None:
                self.logger.warning(
                    f"Async request {handler_name} failed validation "
                    f"(correlation_id: {ctx.correlation_id}): {error.errors()}"
                )
            else:
                self.logger.warning(
                    f"Async request {handler_name} refused "
                    f"(correlation_id: {ctx.correlation_id}): {e}"
                )
        except Exception as e:
            self.logger.error(
                f"Error handling async request {handler_name} "
                f"(correlation_id: {ctx.correlation_id}): {e}"
            )

    # Compatibility aliases
    _get_rpc_semaphore = get_rpc_semaphore
    _get_async_rpc_semaphore = get_async_rpc_semaphore
    _handle_rpc_request = handle_rpc_request
    _handle_describe_request = handle_describe_request
    _handle_async_request = handle_async_request
