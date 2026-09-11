"""The internal runtime coordinator: connection, dispatch, registry, lifecycle.

Composes isolated subsystems (Registry, Discovery, ConnectionManager,
MessageDispatcher, LifecycleManager) to execute service messaging operations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast

import nats
from loguru import logger
from nats.js import JetStreamContext
from pydantic import BaseModel

from .connection import (
    _CLOSED_STOP_TIMEOUT,
    BrokerConnectionState,
    ConnectionManager,
    redact_nats_url,
)
from .discovery import HandlerDiscovery
from .dispatcher import DispatchOutcome, MessageDispatcher, _JetStreamHeartbeat
from .extension import Extension, ExtensionSetupContext, WorkerContext
from .jetstream import consumer_config_for, ensure_streams
from .lifecycle import LifecycleHooks, LifecycleManager
from .registry import ServiceRegistry
from .service_config import ServiceConfig
from .subjects import subject_matches
from .typed_rpc import HandlerSpec

__all__ = [
    "Container",
    "BrokerConnectionState",
    "DispatchOutcome",
    "redact_nats_url",
    "_CLOSED_STOP_TIMEOUT",
    "_JetStreamHeartbeat",
]


class Container:
    """Internal runtime engine coordinating messaging, connection, and lifecycle.

    Invariants:
    - Composed on CliffracerService upon construction.
    - Manages isolated sub-components via explicit ownership.
    - Eliminates circular callback execution by dispatching directly from NATS to Dispatcher.
    """

    def __init__(self, service: Any, config: ServiceConfig) -> None:
        self.service = service
        self.config = config
        self._logger = getattr(service, "logger", None) or logger.bind(
            service=config.name,
            service_type=service.__class__.__name__ if service else "Container",
        )

        # 1. Registry
        self.registry = ServiceRegistry()

        # 2. Extensions and entrypoints
        self.extensions: list[Extension] = []
        self._entrypoint_kinds: dict[str, Callable[..., Any]] = {}
        self._extensions_set_up = False
        self._handlers_discovered = False

        # 3. Connection Manager
        self.connection = ConnectionManager(
            config=config,
            logger=self.logger,
            on_closed_handler=self.stop,
            is_running_fn=lambda: self.is_running,
            logger_provider=lambda: self.logger,
        )

        # 4. Message Dispatcher
        self.dispatcher = MessageDispatcher(
            registry=self.registry,
            config=config,
            connection_provider=lambda: self.connection,
            extensions=self.extensions,
            logger=self.logger,
            service=self.service,
            task_spawner=self._spawn_supervised_task,
        )
        self.dispatcher.container = self
        self.rpc_dispatcher = self.dispatcher.rpc
        self.event_dispatcher = self.dispatcher.events
        self.jetstream_dispatcher = self.dispatcher.jetstream
        self.dlq_publisher = self.dispatcher.dlq
        self.extension_pipeline = self.dispatcher.pipeline
        self.outbound_dispatcher = self.dispatcher.outbound

        # 5. Lifecycle Manager
        def _get_svc_method(
            method_name: str, default_callable: Callable[..., Any]
        ) -> Callable[..., Any]:
            if self.service is not None and hasattr(self.service, method_name):
                return cast(Callable[..., Any], getattr(self.service, method_name))
            return default_callable

        hooks = LifecycleHooks(
            setup_extensions=lambda: _get_svc_method("_setup_extensions", self._setup_extensions)(),
            discover_handlers=self.discover_handlers,
            connect=lambda: self.service.connect() if self.service is not None else self.connect(),
            ensure_streams=self._ensure_streams,
            validate_dlq=lambda: HandlerDiscovery.validate_dlq_coverage(self.config),
            is_jetstream_active=lambda: self.connection.jetstream_active,
            on_startup=self._on_service_startup,
            start_extensions=lambda: _get_svc_method("_start_extensions", self._start_extensions)(),
            start_health_listener=self._start_health_listener,
            start_timers=self._start_timers,
            setup_subscriptions=lambda: _get_svc_method(
                "_setup_subscriptions", self.setup_subscriptions
            )(),
            stop_timers=lambda: _get_svc_method("_stop_timers", self._stop_timers)(),
            stop_health_listener=self._stop_health_listener,
            cancel_subscriptions=lambda: self.connection.unsubscribe_all(),
            on_shutdown=self._on_service_shutdown,
            stop_extensions=lambda: _get_svc_method("_stop_extensions", self._stop_extensions)(),
            disconnect=lambda: self.service.disconnect()
            if self.service is not None
            else self.disconnect(),
        )
        self.lifecycle = LifecycleManager(
            config=config,
            hooks=hooks,
            logger=self.logger,
        )

    @property
    def logger(self) -> Any:
        return getattr(self.service, "logger", self._logger)

    @logger.setter
    def logger(self, value: Any) -> None:
        self._logger = value

    # ---- Connection Delegations ----

    @property
    def nc(self) -> nats.NATS | None:
        return self.connection.nc

    @nc.setter
    def nc(self, value: nats.NATS | None) -> None:
        self.connection.nc = value

    @property
    def js(self) -> JetStreamContext | None:
        return self.connection.js

    @js.setter
    def js(self, value: JetStreamContext | None) -> None:
        self.connection.js = value

    @property
    def broker_state(self) -> BrokerConnectionState:
        return self.connection.broker_state

    @property
    def is_broker_connected(self) -> bool:
        return self.connection.is_broker_connected

    @property
    def _jetstream_active(self) -> bool:
        return self.connection.jetstream_active

    async def connect(self) -> None:
        await self.connection.connect()

    async def disconnect(self) -> None:
        await self.connection.disconnect()

    # ---- Lifecycle Delegations ----

    @property
    def is_running(self) -> bool:
        return self.lifecycle.is_running

    @property
    def is_stopped(self) -> bool:
        return self.lifecycle.is_stopped

    @property
    def is_starting(self) -> bool:
        return self.lifecycle.is_starting

    @property
    def _stopped(self) -> bool:
        return self.lifecycle.is_stopped

    @_stopped.setter
    def _stopped(self, value: bool) -> None:
        self.lifecycle._stopped = value

    @property
    def _starting(self) -> bool:
        return self.lifecycle.is_starting

    @_starting.setter
    def _starting(self, value: bool) -> None:
        self.lifecycle._starting = value

    @property
    def _active_tasks(self) -> set[asyncio.Task[Any]]:
        return self.lifecycle.active_tasks

    @_active_tasks.setter
    def _active_tasks(self, value: set[asyncio.Task[Any]]) -> None:
        self.lifecycle._active_tasks = value

    @property
    def _subscriptions(self) -> set[asyncio.Task[Any]]:
        return self.connection.subscriptions

    def _spawn_supervised_task(
        self,
        coro: Coroutine[Any, Any, Any] | Awaitable[Any],
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        return self.lifecycle.spawn_supervised_task(coro, name=name)

    async def start(self) -> None:
        await self.lifecycle.start()

    async def stop(self) -> None:
        await self.lifecycle.stop()

    async def _stop_internal(self) -> None:
        await self.lifecycle.stop_internal()

    # ---- Registry Delegations ----

    @property
    def _rpc_handlers(self) -> dict[str, Callable[..., Any]]:
        return self.registry.rpc_handlers

    @property
    def _rpc_specs(self) -> dict[str, HandlerSpec]:
        return self.registry.rpc_specs

    @property
    def _event_handlers(self) -> dict[str, Callable[..., Any]]:
        return self.registry.event_handlers

    @property
    def _event_schemas(self) -> dict[Callable[..., Any], tuple[type[BaseModel], str | None]]:
        return self.registry.event_schemas

    @property
    def _event_durables(self) -> dict[str, str]:
        return self.registry.event_durables

    @property
    def _event_fanout(self) -> set[str]:
        return self.registry.event_fanout

    @property
    def _event_pull(self) -> set[str]:
        return self.registry.event_pull

    @property
    def _event_handler_names(self) -> dict[str, str]:
        return self.registry.event_handler_names

    @property
    def _broadcast_handlers(self) -> dict[str, Callable[..., Any]]:
        return self.registry.broadcast_handlers

    def register_broadcast_handler(self, pattern: str, handler: Callable[..., Any]) -> None:
        """Register a broadcast handler for message patterns."""
        self.registry.broadcast_handlers[pattern] = handler
        self.registry.event_handlers[pattern] = handler
        self.registry.event_fanout.add(pattern)

    @property
    def _timers(self) -> list[Any]:
        return self.registry.timers

    @property
    def _extensions(self) -> list[Extension]:
        return self.extensions

    @_extensions.setter
    def _extensions(self, val: list[Extension]) -> None:
        self.extensions = val

    # ---- Dispatcher Delegations ----

    def _get_rpc_semaphore(self) -> asyncio.Semaphore | None:
        return self.dispatcher._get_rpc_semaphore()

    def _get_async_rpc_semaphore(self) -> asyncio.Semaphore | None:
        return self.dispatcher._get_async_rpc_semaphore()

    def _get_event_semaphore(self) -> asyncio.Semaphore | None:
        return self.dispatcher._get_event_semaphore()

    async def _on_rpc_request(self, msg: Any) -> None:
        sem = self._get_rpc_semaphore()
        if sem is not None:
            await sem.acquire()
            self._spawn_supervised_task(
                self._bounded_handle_rpc(msg, sem),
                name="rpc_bounded_request",
            )
        else:
            self._spawn_supervised_task(
                self._handle_rpc_request(msg),
                name="rpc_request",
            )

    async def _bounded_handle_rpc(self, msg: Any, sem: asyncio.Semaphore) -> None:
        try:
            await self._handle_rpc_request(msg)
        except Exception as e:
            self.logger.debug(f"RPC request failed: {e}")
        finally:
            sem.release()

    async def _on_describe_request(self, msg: Any) -> None:
        await self.dispatcher.on_describe_request(msg)

    async def _on_async_request(self, msg: Any) -> None:
        await self.dispatcher.on_async_request(msg)

    def _make_event_callback(self, pattern: str) -> Callable[[Any], Awaitable[None]]:
        async def _cb(msg: Any) -> None:
            sem = self._get_event_semaphore()
            if sem is not None:
                await sem.acquire()
                self._spawn_supervised_task(
                    self._bounded_handle_event(msg, pattern, sem),
                    name=f"event_bounded:{pattern}",
                )
            else:
                self._spawn_supervised_task(
                    self._dispatch_event(msg, pattern=pattern, raise_on_error=False),
                    name=f"event:{pattern}",
                )

        return _cb

    async def _bounded_handle_event(self, msg: Any, pattern: str, sem: asyncio.Semaphore) -> None:
        try:
            await self._dispatch_event(msg, pattern=pattern, raise_on_error=False)
        finally:
            sem.release()

    def _make_jetstream_event_callback(self, pattern: str) -> Callable[[Any], Awaitable[None]]:
        return self.dispatcher.make_jetstream_event_callback(pattern)

    async def _handle_rpc_request(self, msg: Any) -> None:
        await self.dispatcher.handle_rpc_request(msg)

    async def _handle_async_request(self, msg: Any) -> None:
        await self.dispatcher.handle_async_request(msg)

    async def _handle_describe_request(self, msg: Any) -> None:
        await self.dispatcher.handle_describe_request(msg)

    async def _dispatch_event(
        self, msg: Any, *, pattern: str | None = None, raise_on_error: bool = False
    ) -> DispatchOutcome:
        return await self.dispatcher.handle_event(
            msg, pattern=pattern, raise_on_error=raise_on_error
        )

    @property
    def _handle_event(self) -> Any:
        return self.dispatcher.handle_event

    @_handle_event.setter
    def _handle_event(self, value: Any) -> None:
        self.dispatcher.handle_event = value  # type: ignore[method-assign]
        if hasattr(self.dispatcher, "events"):
            self.dispatcher.events.handle_event = value  # type: ignore[method-assign]

    @_handle_event.deleter
    def _handle_event(self) -> None:
        pass

    @property
    def _handle_jetstream_event(self) -> Any:
        return self.dispatcher.handle_jetstream_event

    @_handle_jetstream_event.setter
    def _handle_jetstream_event(self, value: Any) -> None:
        self.dispatcher.handle_jetstream_event = value  # type: ignore[method-assign]
        if hasattr(self.dispatcher, "jetstream"):
            self.dispatcher.jetstream.handle_jetstream_event = value  # type: ignore[method-assign]

    @_handle_jetstream_event.deleter
    def _handle_jetstream_event(self) -> None:
        pass

    @property
    def _safe_ack(self) -> Any:
        return self.dispatcher._safe_ack

    @_safe_ack.setter
    def _safe_ack(self, value: Any) -> None:
        self.dispatcher._safe_ack = value  # type: ignore[method-assign]
        if hasattr(self.dispatcher, "jetstream"):
            self.dispatcher.jetstream.safe_ack = value  # type: ignore[method-assign]

    @_safe_ack.deleter
    def _safe_ack(self) -> None:
        pass

    @property
    def _safe_nak(self) -> Any:
        return self.dispatcher._safe_nak

    @_safe_nak.setter
    def _safe_nak(self, value: Any) -> None:
        self.dispatcher._safe_nak = value  # type: ignore[method-assign]
        if hasattr(self.dispatcher, "jetstream"):
            self.dispatcher.jetstream.safe_nak = value  # type: ignore[method-assign]

    @_safe_nak.deleter
    def _safe_nak(self) -> None:
        pass

    @property
    def _safe_term(self) -> Any:
        return self.dispatcher._safe_term

    @_safe_term.setter
    def _safe_term(self, value: Any) -> None:
        self.dispatcher._safe_term = value  # type: ignore[method-assign]
        if hasattr(self.dispatcher, "jetstream"):
            self.dispatcher.jetstream.safe_term = value  # type: ignore[method-assign]

    @_safe_term.deleter
    def _safe_term(self) -> None:
        pass

    @property
    def _safe_in_progress(self) -> Any:
        return self.dispatcher._safe_in_progress

    @_safe_in_progress.setter
    def _safe_in_progress(self, value: Any) -> None:
        self.dispatcher._safe_in_progress = value  # type: ignore[method-assign]
        if hasattr(self.dispatcher, "jetstream"):
            self.dispatcher.jetstream.safe_in_progress = value  # type: ignore[method-assign]

    @_safe_in_progress.deleter
    def _safe_in_progress(self) -> None:
        pass

    async def _dead_letter_terminated(self, msg: Any, error: Any, num_delivered: int) -> None:
        await self.dispatcher._dead_letter_terminated(msg, error, num_delivered)

    async def _dead_letter_decode_error(self, msg: Any, error: Exception) -> None:
        await self.dispatcher._dead_letter_decode_error(msg, error)

    @property
    def _publish_dlq(self) -> Any:
        return self.dispatcher.dlq.publish_dlq

    @_publish_dlq.setter
    def _publish_dlq(self, value: Any) -> None:
        self.dispatcher.dlq.publish_dlq = value  # type: ignore[method-assign]

    @_publish_dlq.deleter
    def _publish_dlq(self) -> None:
        pass

    async def _pull_once(self, sub: Any, *, pattern: str | None = None) -> int:
        return await self.dispatcher.pull_once(sub, pattern=pattern)

    async def _pull_loop(self, sub: Any, durable: str, *, pattern: str | None = None) -> None:
        await self.dispatcher.pull_loop(
            sub,
            durable,
            pattern=pattern,
            is_running_fn=lambda: getattr(self.service, "_running", self.is_running),
        )

    async def _report_consumer_drift(self, sub: Any, durable: str) -> None:
        await self.dispatcher.report_consumer_drift(sub, durable)

    def _format_dlq_subject(self) -> str:
        return self.dispatcher.format_dlq_subject()

    def _assert_dlq_covered(self) -> None:
        HandlerDiscovery.validate_dlq_coverage(self.config)

    def _with_namespace(self, subject: str) -> str:
        return HandlerDiscovery.with_namespace(self.config, subject)

    def _effective_event_subject(self, pattern: str, cross_namespace: bool) -> str:
        return HandlerDiscovery.effective_event_subject(self.config, pattern, cross_namespace)

    def _subject_matches(self, pattern: str, subject: str) -> bool:
        return subject_matches(pattern, subject)

    @property
    def _run_worker(self) -> Any:
        return self.dispatcher._run_worker

    @_run_worker.setter
    def _run_worker(self, value: Any) -> None:
        self.dispatcher._run_worker = value  # type: ignore[method-assign]
        if hasattr(self.dispatcher, "rpc"):
            self.dispatcher.rpc._run_worker = value  # type: ignore[method-assign]

    @_run_worker.deleter
    def _run_worker(self) -> None:
        pass

    async def _run_send_hooks(self, ctx: WorkerContext, send: Callable[[], Awaitable[Any]]) -> Any:
        return await self.dispatcher._run_send_hooks(ctx, send)

    def _send_context(
        self, kind: str, subject: str, payload: dict[str, Any], correlation_id: str
    ) -> WorkerContext:
        return self.dispatcher._send_context(kind, subject, payload, correlation_id)

    # ---- Internal Coordination Hooks ----

    def discover_handlers(self) -> None:
        if self._handlers_discovered:
            return
        self._handlers_discovered = True
        HandlerDiscovery.discover(
            self.service,
            self.config,
            extensions=self.extensions,
            entrypoint_kinds=self._entrypoint_kinds,
            registry=self.registry,
        )

    def _bind_extension(self, ext: Extension, name: str) -> Extension:
        bound = ext.bind(self.service, name)
        setattr(self.service, name, bound)
        self.extensions.append(bound)
        for kind, binder in bound.entrypoint_kinds().items():
            if kind in self._entrypoint_kinds:
                raise TypeError(f"entrypoint kind {kind!r} registered twice ({name})")
            self._entrypoint_kinds[kind] = binder
        return bound

    async def _setup_extensions(self) -> None:
        if self._extensions_set_up:
            return
        self._extensions_set_up = True
        for ext in self.extensions:
            ctx = ExtensionSetupContext(
                service_config=self.config,
                broker_url=self.config.nats_url,
                service=self.service,
            )
            await ext.setup(ctx)

    async def _start_extensions(self) -> None:
        for ext in self.extensions:
            await ext.start()

    async def _stop_extensions(self) -> None:
        for ext in reversed(self.extensions):
            try:
                await ext.stop()
            except Exception as exc:
                self.logger.error(f"extension {ext.name} failed to stop: {exc}")
        self._extensions_set_up = False

    async def setup_extensions(self) -> None:
        """Initialize all registered extensions."""
        await self._setup_extensions()

    async def start_extensions(self) -> None:
        """Start all registered extensions."""
        await self._start_extensions()

    async def stop_extensions(self) -> None:
        """Stop all registered extensions in reverse order."""
        await self._stop_extensions()

    async def _ensure_streams(self) -> None:
        if self.connection.jetstream_active:
            await ensure_streams(
                self.connection.js,
                self.config.jetstream_streams,
                allow_update=self.config.jetstream_update_streams,
                logger=self.logger,
            )

    async def _on_service_startup(self) -> None:
        if hasattr(self.service, "on_startup") and callable(self.service.on_startup):
            await self.service.on_startup()

    async def _on_service_shutdown(self) -> None:
        if hasattr(self.service, "on_shutdown") and callable(self.service.on_shutdown):
            await self.service.on_shutdown()

    async def _start_health_listener(self) -> None:
        hl = getattr(self.service, "health_listener", None)
        if hl is not None and hasattr(hl, "start") and callable(hl.start):
            await hl.start()

    async def _stop_health_listener(self) -> None:
        hl = getattr(self.service, "health_listener", None)
        if hl is not None and hasattr(hl, "stop") and callable(hl.stop):
            await hl.stop()

    async def _start_timers(self) -> None:
        for timer_instance in self.registry.timers:
            await timer_instance.start(self.service)

    async def _stop_timers(self) -> None:
        for timer_instance in self.registry.timers:
            await timer_instance.stop()

    async def _setup_subscriptions(self) -> None:
        assert self.connection.nc is not None

        # RPC subscription
        rpc_subject = self._with_namespace(f"{self.config.name}.rpc.*")
        rpc_queue = self._with_namespace(f"{self.config.name}.rpc")
        sub = await self.connection.nc.subscribe(
            rpc_subject, queue=rpc_queue, cb=self.dispatcher.on_rpc_request
        )
        self.connection.subscriptions.add(asyncio.create_task(self._subscription_handler(sub)))

        # Describe subscription
        desc_subject = self._with_namespace(f"{self.config.name}.describe")
        desc_queue = self._with_namespace(f"{self.config.name}.rpc")
        sub = await self.connection.nc.subscribe(
            desc_subject, queue=desc_queue, cb=self.dispatcher.on_describe_request
        )
        self.connection.subscriptions.add(asyncio.create_task(self._subscription_handler(sub)))

        # Async RPC subscription
        async_subject = self._with_namespace(f"{self.config.name}.async.*")
        async_queue = self._with_namespace(f"{self.config.name}.async")
        sub = await self.connection.nc.subscribe(
            async_subject, queue=async_queue, cb=self.dispatcher.on_async_request
        )
        self.connection.subscriptions.add(asyncio.create_task(self._subscription_handler(sub)))

        # Event subscriptions
        for pattern in self.registry.event_handlers:
            durable = self.registry.event_durables.get(pattern)
            if self.connection.jetstream_active and durable and pattern in self.registry.event_pull:
                assert self.connection.js is not None
                pull_sub = await self.connection.js.pull_subscribe(
                    pattern,
                    durable=durable,
                    config=consumer_config_for(self.config),
                )
                await self.dispatcher.report_consumer_drift(pull_sub, durable)
                self.connection.subscriptions.add(
                    asyncio.create_task(
                        self.dispatcher.pull_loop(
                            pull_sub,
                            durable,
                            pattern=pattern,
                            is_running_fn=lambda: self.is_running,
                        )
                    )
                )
                continue
            if self.connection.jetstream_active and durable:
                assert self.connection.js is not None
                sub = await self.connection.js.subscribe(
                    pattern,
                    queue=durable,
                    cb=self.dispatcher.make_jetstream_event_callback(pattern),
                    durable=durable,
                    manual_ack=True,
                    config=consumer_config_for(self.config),
                )
                await self.dispatcher.report_consumer_drift(sub, durable)
            else:
                sub = await self.connection.nc.subscribe(
                    pattern, cb=self.dispatcher.make_event_callback(pattern)
                )
            self.connection.subscriptions.add(asyncio.create_task(self._subscription_handler(sub)))

        await self.connection.nc.flush()

    async def setup_subscriptions(self) -> None:
        """Set up all NATS subscriptions for RPC and event handlers."""
        await self._setup_subscriptions()

    async def _subscription_handler(self, sub: Any) -> None:
        try:
            while self.is_running:
                await asyncio.sleep(1)
        finally:
            if (
                self.connection.nc
                and self.connection.broker_state != BrokerConnectionState.CLOSED
                and not getattr(self.connection.nc, "is_draining", False)
            ):
                try:
                    await sub.unsubscribe()
                except Exception:
                    pass
