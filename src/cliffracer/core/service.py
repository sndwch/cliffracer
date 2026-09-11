"""The CliffracerService application runtime façade.

Provides the high-level application service base class, orchestrating configuration,
health listeners, extension declarations, and outbound messaging client calls
through an internal Container coordinator.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import nats
from loguru import logger
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js import JetStreamContext

from cliffracer.core.dependencies import (
    DEFAULT_TIMEOUT,
    Dependency,
    check_dependencies,
    failed_dependencies,
    reject_removed_detail,
)

from .connection import (
    _CLOSED_STOP_TIMEOUT,
    BrokerConnectionState,
    redact_nats_url,
)
from .container import Container, DispatchOutcome
from .correlation import CorrelationContext
from .correlation_extension import CorrelationExtension
from .decorators import _unusable_subject_reason
from .discovery import HandlerDiscovery
from .exceptions import RPCError, RPCTimeoutError
from .extension import Extension
from .health_listener import HealthListener
from .idempotency import IdempotencyContext, compute_payload_hash, format_nats_msg_id
from .jetstream import StreamDeclarationError, subject_covered_by
from .service_config import ServiceConfig
from .validation import deserialize_payload, serialize_payload
from .validation_extension import ValidationExtension

__all__ = [
    "CliffracerService",
    "Container",
    "BrokerConnectionState",
    "DispatchOutcome",
    "redact_nats_url",
    "_CLOSED_STOP_TIMEOUT",
]


class CliffracerService:
    """Application runtime façade executing over NATS.

    Invariants:
    - Instantiates and owns a Container coordinating isolated runtime components.
    - Contains no low-level dispatch callbacks or connection transport logic.
    - Outbound messaging executes send hooks through the container dispatcher.
    - Subclasses declare handlers via decorators (@rpc, @listener, @timer).
    """

    _HANDLER_MARKER_PREFIX = "_cliffracer_"

    def _register_event_handler(self, subject: Any, method: Callable[..., Any]) -> None:
        """Register one event handler, refusing a subject that is not a string."""
        if not isinstance(subject, str):
            raise TypeError(
                f"{type(self).__name__}: event subject must be a str, got "
                f"{type(subject).__name__} ({subject!r}) from handler "
                f"{getattr(method, '__name__', method)!r}"
            )
        self.container.registry.event_handlers[subject] = method

    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.logger = logger.bind(
            service=self.config.name,
            service_type=self.__class__.__name__,
        )

        # Construct internal container coordinator
        self._container = Container(self, config)

        # Dependencies registered on this service
        self._dependencies: list[Dependency] = []
        self._register_dependencies()

        # Collect and bind extensions
        self._collect_extensions()

        # Built-in HTTP health probe listener
        self.health_listener = HealthListener(
            self,
            config.health_host,
            config.health_port,
            port_is_explicit="health_port" in config.model_fields_set,
        )
        if not config.health_listener:
            self.health_listener.disable("health_listener=False")

    @property
    def container(self) -> Container:
        """Internal container coordinator."""
        return self._container

    # ---- Public Broker State Properties ----

    @property
    def is_broker_connected(self) -> bool:
        """Indicates whether the NATS connection is in CONNECTED state."""
        return self.container.is_broker_connected

    @property
    def broker_state(self) -> BrokerConnectionState:
        """Current NATS connection state."""
        return self.container.broker_state

    @property
    def nc(self) -> nats.NATS | None:
        """The underlying NATS client connection instance."""
        return self.container.nc

    @nc.setter
    def nc(self, value: nats.NATS | None) -> None:
        self.container.nc = value

    @property
    def js(self) -> JetStreamContext | None:
        """The underlying JetStream context instance."""
        return self.container.js

    @js.setter
    def js(self, value: JetStreamContext | None) -> None:
        self.container.js = value

    @property
    def _running(self) -> bool:
        """Internal running flag reflecting lifecycle state."""
        return self.container.is_running

    @_running.setter
    def _running(self, value: bool) -> None:
        self.container.lifecycle._running = value

    @property
    def _starting(self) -> bool:
        """Internal starting flag reflecting lifecycle state."""
        return self.container.lifecycle.is_starting

    @_starting.setter
    def _starting(self, value: bool) -> None:
        self.container.lifecycle._starting = value

    @property
    def _stopped(self) -> bool:
        """Internal stopped flag reflecting lifecycle state."""
        return self.container.lifecycle.is_stopped

    @_stopped.setter
    def _stopped(self, value: bool) -> None:
        self.container.lifecycle._stopped = value

    @property
    def _startup_succeeded(self) -> bool:
        """Internal flag indicating whether startup procedure completed successfully."""
        return self.container.lifecycle._startup_succeeded

    @_startup_succeeded.setter
    def _startup_succeeded(self, value: bool) -> None:
        self.container.lifecycle._startup_succeeded = value

    @property
    def _on_startup_completed(self) -> bool:
        """Internal flag indicating whether on_startup callback completed."""
        return self.container.lifecycle._on_startup_completed

    @_on_startup_completed.setter
    def _on_startup_completed(self, value: bool) -> None:
        self.container.lifecycle._on_startup_completed = value

    @property
    def _timers(self) -> list[Any]:
        """Discovered timer specifications."""
        return self.container.registry.timers

    # ---- Lifecycle Methods ----

    async def connect(self) -> None:
        """Establish connection to NATS via the container."""
        await self.container.connect()

    async def disconnect(self) -> None:
        """Disconnect from NATS via the container."""
        await self.container.disconnect()

    async def start(self) -> None:
        """Execute service startup sequence."""
        await self.container.start()

    async def stop(self) -> None:
        """Execute service shutdown sequence."""
        await self.container.stop()

    async def _stop_internal(self) -> None:
        """Internal stop routine executing serialized resource cleanup."""
        await self.container.lifecycle.stop_internal()

    def register_broadcast_handler(self, pattern: str, handler: Callable[..., Any]) -> None:
        """Register a broadcast handler for message patterns."""
        self.container.register_broadcast_handler(pattern, handler)

    def run(self) -> None:
        """Run the service synchronously, blocking until interrupted."""

        async def _run() -> None:
            try:
                await self.start()
                while self._running:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                self.logger.info("Received interrupt signal")
            finally:
                await self.stop()

        asyncio.run(_run())

    async def on_startup(self) -> None:
        """Hook executed during start() after NATS connection and stream setup."""

    async def on_shutdown(self) -> None:
        """Hook executed during stop() before extension teardown and disconnect."""

    def _discover_handlers(self) -> None:
        """Trigger handler discovery across the service instance."""
        self.container.discover_handlers()

    # ---- Extension Registration ----

    def _collect_extensions(self) -> None:
        self.container._bind_extension(CorrelationExtension(), "_correlation")
        self.container._bind_extension(ValidationExtension(), "_validation")

        declared: dict[str, Extension] = {}
        for klass in reversed(type(self).__mro__):
            for attr, value in vars(klass).items():
                if isinstance(value, Extension):
                    declared[attr] = value
        for attr, value in declared.items():
            self.container._bind_extension(value, attr)

    @property
    def extensions(self) -> list[Extension]:
        """Registered extension instances bound to this service."""
        return self.container.extensions

    @property
    def _extensions(self) -> list[Extension]:
        """Registered extension instances bound to this service."""
        return self.container.extensions

    def add_extension(self, ext: Extension, name: str | None = None) -> Extension:
        """Register an extension on this instance before start()."""
        if self.container._extensions_set_up:
            raise RuntimeError("add_extension must be called before start()")
        return self.container._bind_extension(ext, name or type(ext).__name__.lower())

    # ---- Dependency Probing ----

    def _register_dependencies(self) -> None:
        HandlerDiscovery.discover_dependencies(self, self.container.registry)
        self._dependencies = list(self.container.registry.dependencies)

    def add_dependency(
        self,
        name: str,
        probe: Callable[[], Any],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        **detail: Any,
    ) -> None:
        """Register a runtime dependency probe."""
        reject_removed_detail(detail)
        self._dependencies = [dep for dep in self._dependencies if dep.name != name]
        self._dependencies.append(
            Dependency(name=name, probe=probe, timeout=timeout, detail=dict(detail))
        )
        self._dependencies.sort(key=lambda dep: dep.name)
        self.container.registry.dependencies = list(self._dependencies)

    # ---- Health & Inspection Probes ----

    def liveness_check(self) -> dict[str, Any]:
        """Perform a lightweight process liveness check."""
        return {
            "service": self.config.name,
            "status": "healthy" if self._running else "stopped",
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def is_live(self) -> dict[str, Any]:
        """Alias for liveness_check."""
        return self.liveness_check()

    async def health_check(self) -> dict[str, Any]:
        """Perform readiness health check evaluating broker and dependency probes."""
        is_connected = self.is_broker_connected
        dependencies, dependencies_error = await self._check_dependencies()
        broken = failed_dependencies(dependencies)

        if not self._running:
            status = "stopped"
        elif self.broker_state == BrokerConnectionState.CONNECTING:
            status = "connecting"
        elif (
            self.broker_state in (BrokerConnectionState.DISCONNECTED, BrokerConnectionState.CLOSED)
            or not is_connected
        ):
            status = "disconnected"
        elif broken or dependencies_error:
            status = "unhealthy"
        else:
            status = "healthy"

        health: dict[str, Any] = {
            "service": self.config.name,
            "status": status,
            "timestamp": datetime.now(UTC).isoformat(),
            "nats_connected": is_connected if self.nc else None,
            "broker_state": self.broker_state.value,
            "features": self.container.registry.feature_counts(),
        }

        if dependencies:
            health["dependencies"] = dependencies
            if broken:
                health["unhealthy_dependencies"] = broken
        if dependencies_error:
            health["dependencies_error"] = dependencies_error

        try:
            for ext in self.container.extensions:
                if ext.name.startswith("_"):
                    continue
                try:
                    contribution = ext.health_details()
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(f"health_details of extension {ext.name} failed: {exc}")
                    health[ext.name] = {"error": f"{type(exc).__name__}: {exc}"}
                    continue
                if contribution is not None:
                    health[ext.name] = contribution
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"Health details unavailable: {exc}")
            health["details_error"] = str(exc)

        return health

    async def _check_dependencies(self) -> tuple[dict[str, dict[str, Any]], str | None]:
        try:
            return await check_dependencies(self._dependencies), None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"Dependency checks unavailable: {exc}")
            return {}, f"{type(exc).__name__}: {exc}"

    def get_service_info(self) -> dict[str, Any]:
        """Return diagnostic metadata and discovered handler endpoints."""
        info = {
            "name": self.config.name,
            "version": self.config.version,
            "rpc_methods": list(self.container.registry.rpc_handlers.keys()),
            "event_patterns": list(self.container.registry.event_handlers.keys()),
            "timer_methods": [timer.method_name for timer in self.container.registry.timers],
            "subjects": {
                "rpc": HandlerDiscovery.with_namespace(self.config, f"{self.config.name}.rpc.*"),
                "events": f"{self.config.name}.events.*",
            },
        }

        for ext in self.container.extensions:
            if ext.name.startswith("_"):
                continue
            try:
                contribution = ext.info_details()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"info_details of extension {ext.name} failed: {exc}")
                continue
            if contribution is not None:
                info[ext.name] = contribution

        return info

    def get_timer_stats(self) -> dict[str, Any]:
        """Return execution statistics for all active timers."""
        timers = self.container.registry.timers
        return {
            "timer_count": len(timers),
            "timers": [timer.get_stats() for timer in timers],
        }

    # ---- Outbound Messaging Client Methods ----

    async def call_rpc(
        self, service: str, method: str, *, namespace: str | None = None, **kwargs: Any
    ) -> Any:
        """Call an RPC method on another service, awaiting response."""
        target_ns = namespace if namespace is not None else self.config.namespace
        subject = f"{service}.rpc.{method}"
        if target_ns:
            subject = f"{target_ns}.{subject}"
        reason = _unusable_subject_reason(subject)
        if reason is not None:
            raise ValueError(f"Invalid RPC subject {subject!r}: {reason}")

        if "correlation_id" not in kwargs:
            kwargs["correlation_id"] = (
                CorrelationContext.get() or CorrelationContext.get_or_create_id()
            )

        correlation_id = kwargs["correlation_id"]
        self.logger.info(f"Calling RPC {service}.{method} with correlation_id: {correlation_id}")

        request_data, content_type = serialize_payload(
            kwargs, format=self.config.serialization_format
        )
        ctx = self.container.dispatcher._send_context("call_rpc", subject, kwargs, correlation_id)
        ctx.headers["Content-Type"] = content_type

        async def _send() -> Any:
            try:
                assert self.nc is not None
                response = await self.nc.request(
                    subject,
                    request_data,
                    timeout=self.config.request_timeout,
                    headers=dict(ctx.headers),
                )

                resp_h = getattr(response, "headers", None)
                reply_headers = dict(resp_h) if isinstance(resp_h, Mapping) else {}
                reply_ct = None
                for k, v in reply_headers.items():
                    if k.lower() == "content-type":
                        reply_ct = v
                        break

                response_data = deserialize_payload(
                    response.data,
                    content_type=reply_ct,
                    fallback_format=self.config.serialization_format,
                )

                if "error" in response_data:
                    raise RPCError(
                        f"RPC Error calling {service}.{method}: {response_data['error']}",
                        details=response_data.get("details"),
                    )

                return response_data.get("result")

            except NatsTimeoutError as e:
                self.logger.error(
                    f"RPC timeout calling {service}.{method} (correlation_id: {correlation_id})"
                )
                raise RPCTimeoutError(f"RPC timeout calling {service}.{method}") from e

        return await self.container.dispatcher._run_send_hooks(ctx, _send)

    async def call_async(
        self, service: str, method: str, *, namespace: str | None = None, **kwargs: Any
    ) -> Any:
        """Call an RPC method asynchronously without awaiting a reply."""
        target_ns = namespace if namespace is not None else self.config.namespace
        subject = f"{service}.async.{method}"
        if target_ns:
            subject = f"{target_ns}.{subject}"
        reason = _unusable_subject_reason(subject)
        if reason is not None:
            raise ValueError(f"Invalid async subject {subject!r}: {reason}")

        if "correlation_id" not in kwargs:
            kwargs["correlation_id"] = (
                CorrelationContext.get() or CorrelationContext.get_or_create_id()
            )

        self.logger.info(
            f"Calling async {service}.{method} with correlation_id: {kwargs['correlation_id']}"
        )

        request_data, content_type = serialize_payload(
            kwargs, format=self.config.serialization_format
        )
        ctx = self.container.dispatcher._send_context(
            "call_async", subject, kwargs, kwargs["correlation_id"]
        )
        ctx.headers["Content-Type"] = content_type

        async def _send() -> None:
            assert self.nc is not None
            await self.nc.publish(subject, request_data, headers=dict(ctx.headers))

        return await self.container.dispatcher._run_send_hooks(ctx, _send)

    async def call_rpc_no_wait(
        self, service: str, method: str, *, namespace: str | None = None, **kwargs: Any
    ) -> Any:
        """Call an RPC method without waiting for response."""
        target_ns = namespace if namespace is not None else self.config.namespace
        subject = f"{service}.rpc.{method}"
        if target_ns:
            subject = f"{target_ns}.{subject}"
        reason = _unusable_subject_reason(subject)
        if reason is not None:
            raise ValueError(f"Invalid RPC subject {subject!r}: {reason}")

        if "correlation_id" not in kwargs:
            kwargs["correlation_id"] = (
                CorrelationContext.get() or CorrelationContext.get_or_create_id()
            )

        request_data, content_type = serialize_payload(
            kwargs, format=self.config.serialization_format
        )
        ctx = self.container.dispatcher._send_context(
            "call_rpc_no_wait", subject, kwargs, kwargs["correlation_id"]
        )
        ctx.headers["Content-Type"] = content_type

        async def _send() -> None:
            assert self.nc is not None
            await self.nc.publish(subject, request_data, headers=dict(ctx.headers))

        return await self.container.dispatcher._run_send_hooks(ctx, _send)

    async def publish_event(
        self,
        subject: str,
        *,
        envelope: bool = True,
        idempotency_key: str | None = None,
        idempotent: bool | None = None,
        **kwargs: Any,
    ) -> Any:
        """Publish an event under this service namespace."""
        cid = (
            kwargs.pop("correlation_id", None)
            or CorrelationContext.get()
            or CorrelationContext.get_or_create_id()
        )

        domain_data = (
            kwargs["data"]
            if (len(kwargs) == 1 and "data" in kwargs and isinstance(kwargs["data"], dict))
            else kwargs
        )

        if envelope:
            payload: dict[str, Any] = {
                **kwargs,
                "data": domain_data,
                "timestamp": datetime.now(UTC).isoformat(),
                "source_service": self.config.name,
                "correlation_id": cid,
            }
        else:
            payload = dict(kwargs)
            payload["correlation_id"] = cid

        self.logger.info(f"Publishing event {subject} with correlation_id: {cid}")

        full_subject = HandlerDiscovery.with_namespace(self.config, subject)
        ctx = self.container.dispatcher._send_context("publish_event", full_subject, payload, cid)

        # Check explicit idempotency_key, ambient IdempotencyContext, or config / parameter
        key_candidate = idempotency_key or IdempotencyContext.get()
        if key_candidate is None and (
            idempotent or getattr(self.config, "idempotent_publishing", False)
        ):
            key_candidate = compute_payload_hash(domain_data)

        if key_candidate is not None:
            ctx.headers["Nats-Msg-Id"] = format_nats_msg_id(
                full_subject, str(key_candidate), hash_payload=(idempotent is True)
            )

        return await self.container.dispatcher._run_send_hooks(
            ctx, lambda: self._publish_event_unhooked(full_subject, payload, dict(ctx.headers))
        )

    async def _publish_event_unhooked(
        self, full_subject: str, kwargs: dict[str, Any], headers: dict[str, Any]
    ) -> Any:
        """Publish directly to NATS without executing send hooks."""
        event_data, content_type = serialize_payload(
            kwargs, format=self.config.serialization_format
        )
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = content_type

        if self.container._jetstream_active:
            if not subject_covered_by(self.config.jetstream_streams, full_subject):
                claims = [s for spec in self.config.jetstream_streams for s in spec.subjects]
                raise StreamDeclarationError(
                    f"jetstream_enabled is on, but no declared stream covers "
                    f"{full_subject!r}. Declared claims: {claims}. Every subject a "
                    f"service publishes needs a stream, including ones nothing consumes."
                )
            assert self.js is not None
            ack = await self.js.publish(full_subject, event_data, headers=headers)
            if getattr(ack, "duplicate", False):
                self.logger.info(
                    f"JetStream detected duplicate publish on {full_subject} with Nats-Msg-Id: "
                    f"{headers.get('Nats-Msg-Id')}"
                )
            return ack

        assert self.nc is not None
        await self.nc.publish(full_subject, event_data, headers=headers)

    async def broadcast_message(self, subject: str, **kwargs: Any) -> None:
        """Publish an event and fan out to WebSocket extensions."""
        cid = (
            kwargs.pop("correlation_id", None)
            or CorrelationContext.get()
            or CorrelationContext.get_or_create_id()
        )
        message: dict[str, Any] = {
            "data": kwargs,
            "timestamp": datetime.now(UTC).isoformat(),
            "source_service": self.config.name,
            "correlation_id": cid,
        }

        full_subject = HandlerDiscovery.with_namespace(self.config, subject)
        ctx = self.container.dispatcher._send_context("broadcast", full_subject, message, str(cid))

        async def _send() -> None:
            await self._publish_event_unhooked(full_subject, message, dict(ctx.headers))

            payload = {
                "type": "broadcast",
                "subject": subject,
                "data": kwargs,
                "timestamp": message["timestamp"],
            }
            for ext in self.container.extensions:
                fan_out = getattr(ext, "broadcast_to_websockets", None)
                if fan_out is not None:
                    await fan_out(payload)

        await self.container.dispatcher._run_send_hooks(ctx, _send)
        self.logger.debug(f"Broadcasted message: {subject}")
