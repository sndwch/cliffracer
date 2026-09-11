"""The inbound and outbound message execution and dispatch pipeline.

Coordinates isolated collaborator classes under cliffracer.core.dispatch
through a composite MessageDispatcher facade.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from loguru import logger as global_logger

from .dispatch import (
    DeadLetterPublisher,
    DispatchOutcome,
    EventDispatcher,
    ExtensionPipeline,
    JetStreamDispatcher,
    OutboundDispatcher,
    RpcDispatcher,
    _HandlerMeta,
    _JetStreamHeartbeat,
)
from .extension import Extension, WorkerContext
from .registry import ServiceRegistry
from .service_config import ServiceConfig

__all__ = [
    "DispatchOutcome",
    "_HandlerMeta",
    "_JetStreamHeartbeat",
    "ExtensionPipeline",
    "DeadLetterPublisher",
    "RpcDispatcher",
    "EventDispatcher",
    "JetStreamDispatcher",
    "OutboundDispatcher",
    "MessageDispatcher",
]


class MessageDispatcher:
    """Composite facade coordinating isolated message dispatch collaborators.

    Invariants:
    - Composes 6 collaborator classes:
      (ExtensionPipeline, DeadLetterPublisher, RpcDispatcher, EventDispatcher,
       JetStreamDispatcher, OutboundDispatcher).
    - Preserves 100% backward compatibility for Container, ServiceTestHarness,
      and all external packages.
    - Zero circular duck-typing checks into container.__dict__.
    - Every class remains <= 500 statements and < 800 lines.
    """

    def __init__(
        self,
        registry: ServiceRegistry,
        config: ServiceConfig,
        connection_provider: Callable[[], Any],
        extensions: list[Extension],
        logger: Any = None,
        service: Any = None,
        task_spawner: Callable[[Coroutine[Any, Any, Any], str | None], asyncio.Task[Any]]
        | None = None,
    ) -> None:
        self.registry = registry
        self.config = config
        self.connection_provider = connection_provider
        self._extensions = extensions
        self.logger = logger or global_logger.bind(service=config.name)
        self.service = service
        self.task_spawner = task_spawner
        self.container: Any | None = None

        # Instantiated isolated collaborators
        self.pipeline = ExtensionPipeline(self._extensions, self.logger)
        self.dlq = DeadLetterPublisher(
            self.config, self.connection_provider, self.logger, self.service
        )
        self.rpc = RpcDispatcher(
            self.registry,
            self.config,
            self.pipeline,
            self.task_spawner,
            self.logger,
            self.service,
        )
        self.events = EventDispatcher(
            self.registry,
            self.config,
            self.pipeline,
            self.dlq,
            self.task_spawner,
            self.logger,
        )
        self.jetstream = JetStreamDispatcher(
            self.config,
            self.connection_provider,
            self.events,
            self.dlq,
            self.task_spawner,
            self.logger,
            self.service,
        )
        self.outbound = OutboundDispatcher(
            self.config, self.connection_provider, self.pipeline, self.logger
        )

    @property
    def extensions(self) -> list[Extension]:
        return self._extensions

    @extensions.setter
    def extensions(self, val: list[Extension]) -> None:
        self._extensions = val
        if hasattr(self, "pipeline"):
            self.pipeline.extensions = val

    # ---- Connection Access ----

    @property
    def nc(self) -> Any:
        return self.dlq.nc

    @property
    def js(self) -> Any:
        return self.dlq.js

    @property
    def _jetstream_active(self) -> bool:
        return self.dlq._jetstream_active

    def _spawn_task(
        self, coro: Coroutine[Any, Any, Any], name: str | None = None
    ) -> asyncio.Task[Any]:
        return self.rpc._spawn_task(coro, name=name)

    # ---- Concurrency Semaphores ----

    def _get_rpc_semaphore(self) -> asyncio.Semaphore | None:
        return self.rpc.get_rpc_semaphore()

    def _get_async_rpc_semaphore(self) -> asyncio.Semaphore | None:
        return self.rpc.get_async_rpc_semaphore()

    def _get_event_semaphore(self) -> asyncio.Semaphore | None:
        return self.events.get_event_semaphore()

    # ---- JetStream Transport Protections ----

    async def _safe_ack(self, msg: Any) -> bool:
        return await self.jetstream.safe_ack(msg)

    async def _safe_nak(self, msg: Any, delay: float = 0.0) -> bool:
        return await self.jetstream.safe_nak(msg, delay=delay)

    async def _safe_term(self, msg: Any) -> bool:
        return await self.jetstream.safe_term(msg)

    async def _safe_in_progress(self, msg: Any) -> bool:
        return await self.jetstream.safe_in_progress(msg)

    safe_ack = _safe_ack
    safe_nak = _safe_nak
    safe_term = _safe_term
    safe_in_progress = _safe_in_progress

    # ---- NATS Subscription Entrypoints ----

    async def on_rpc_request(self, msg: Any) -> None:
        """Handle incoming NATS RPC subscription message."""
        await self.rpc.on_rpc_request(msg)

    async def _bounded_handle_rpc(self, msg: Any, sem: asyncio.Semaphore) -> None:
        await self.rpc._bounded_handle_rpc(msg, sem)

    async def on_describe_request(self, msg: Any) -> None:
        """Handle incoming NATS describe subscription message."""
        await self.rpc.on_describe_request(msg)

    async def on_async_request(self, msg: Any) -> None:
        """Handle incoming NATS fire-and-forget async RPC subscription message."""
        await self.rpc.on_async_request(msg)

    async def _bounded_handle_async_rpc(self, msg: Any, sem: asyncio.Semaphore) -> None:
        await self.rpc._bounded_handle_async_rpc(msg, sem)

    def make_event_callback(self, pattern: str) -> Callable[[Any], Awaitable[None]]:
        """Construct a NATS message callback dispatching core events for a pattern."""
        return self.events.make_event_callback(pattern)

    async def _bounded_handle_event(self, msg: Any, pattern: str, sem: asyncio.Semaphore) -> None:
        await self.events._bounded_handle_event(msg, pattern, sem)

    def make_jetstream_event_callback(self, pattern: str) -> Callable[[Any], Awaitable[None]]:
        """Construct a NATS message callback dispatching JetStream events for a pattern."""
        return self.jetstream.make_event_callback(pattern)

    async def _bounded_handle_jetstream_event(
        self, msg: Any, pattern: str, sem: asyncio.Semaphore
    ) -> None:
        await self.jetstream._bounded_handle_jetstream_event(msg, pattern, sem)

    # ---- Core Dispatch Pipelines ----

    async def handle_rpc_request(self, msg: Any) -> None:
        """Execute RPC dispatch pipeline, validating input and replying with an envelope."""
        await self.rpc.handle_rpc_request(msg)

    async def handle_describe_request(self, msg: Any) -> None:
        """Answer this service's Description metadata in canonical bytes."""
        await self.rpc.handle_describe_request(msg)

    async def handle_async_request(self, msg: Any) -> None:
        """Handle incoming fire-and-forget async RPC requests."""
        await self.rpc.handle_async_request(msg)

    def _get_handler_meta(self, handler: Callable[..., Any]) -> _HandlerMeta:
        return self.events._get_handler_meta(handler)

    async def handle_event(
        self, msg: Any, *, pattern: str | None = None, raise_on_error: bool = False
    ) -> DispatchOutcome:
        """Dispatch an event message to matching handlers."""
        return await self.events.handle_event(msg, pattern=pattern, raise_on_error=raise_on_error)

    async def handle_jetstream_event(self, msg: Any, *, pattern: str | None = None) -> None:
        """JetStream event dispatch with pulse heartbeat, ack, nak, or termination."""
        await self.jetstream.handle_jetstream_event(msg, pattern=pattern)

    async def pull_once(self, sub: Any, *, pattern: str | None = None) -> int:
        """Fetch one batch from a JetStream pull consumer and dispatch messages."""
        return await self.jetstream.pull_once(sub, pattern=pattern)

    async def pull_loop(
        self,
        sub: Any,
        durable: str,
        *,
        pattern: str | None = None,
        is_running_fn: Callable[[], bool] | None = None,
    ) -> None:
        """Continually fetch batches from a JetStream pull consumer while running."""
        await self.jetstream.pull_loop(sub, durable, pattern=pattern, is_running_fn=is_running_fn)

    async def report_consumer_drift(self, sub: Any, durable: str) -> None:
        """Warn when server consumer configuration diverges from service configuration."""
        await self.jetstream.report_consumer_drift(sub, durable)

    # ---- Dead Letter Queue Publishing ----

    def format_dlq_subject(self) -> str:
        """Format configured dead-letter subject template."""
        return self.dlq.format_dlq_subject()

    async def publish_dlq(
        self,
        subject: str,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Publish an unroutable or error diagnostic message to the dead-letter queue."""
        await self.dlq.publish_dlq(subject, payload=payload, headers=headers, **kwargs)

    async def _dead_letter_decode_error(self, msg: Any, error: Exception) -> None:
        await self.dlq.dead_letter_decode_error(msg, error)

    async def _dead_letter_terminated(self, msg: Any, error: Any, num_delivered: int) -> None:
        await self.dlq.dead_letter_terminated(msg, error, num_delivered)

    async def _handle_invalid_message(
        self,
        subject: str,
        payload: Any,
        error: Any,
        schema: Any,
        on_invalid: str | None,
        correlation_id: str | None = None,
    ) -> None:
        await self.dlq.handle_invalid_message(
            subject, payload, error, schema, on_invalid, correlation_id=correlation_id
        )

    dead_letter_decode_error = _dead_letter_decode_error
    dead_letter_terminated = _dead_letter_terminated
    handle_invalid_message = _handle_invalid_message

    # ---- Extension Hook Pipelines ----

    async def _run_worker(self, ctx: WorkerContext, call: Callable[[], Awaitable[Any]]) -> Any:
        """Execute extension worker lifecycle hooks around call."""
        return await self.pipeline.run_worker(ctx, call)

    async def _run_send_hooks(self, ctx: WorkerContext, send: Callable[[], Awaitable[Any]]) -> Any:
        """Execute extension outbound hooks around send."""
        return await self.pipeline.run_send_hooks(ctx, send)

    def _send_context(
        self, kind: str, subject: str, payload: dict[str, Any], correlation_id: str
    ) -> WorkerContext:
        """Construct initialized outbound WorkerContext."""
        return self.outbound.send_context(kind, subject, payload, correlation_id)

    async def _guarded_hook(self, ext: Extension, hook: str, coro: Awaitable[None]) -> None:
        await self.pipeline._guarded_hook(ext, hook, coro)

    run_worker = _run_worker
    run_send_hooks = _run_send_hooks
    send_context = _send_context
