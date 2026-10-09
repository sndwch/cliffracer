"""The CliffracerService application runtime façade.

Provides the high-level application service base class, orchestrating configuration,
health listeners, extension declarations, and outbound messaging client calls
through an internal Container coordinator.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any

import nats
import pydantic_core
from loguru import logger
from nats.js import JetStreamContext

from cliffracer.core.broker_probe import BrokerProbe, ProbeResult
from cliffracer.core.dependencies import (
    DEFAULT_TIMEOUT,
    Dependency,
    check_dependencies,
    failed_dependencies,
    reject_removed_detail,
)
from cliffracer.core.error_text import exception_text

from . import rpc_calls
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
from .dispatch.handler_limits import health_details
from .exceptions import (
    RpcConnectionError,
    ServiceLifecycleError,
)
from .extension import Extension
from .health_listener import HealthListener
from .idempotency import IdempotencyContext, compute_payload_hash, format_nats_msg_id
from .jetstream import StreamDeclarationError, subject_covered_by
from .lifecycle import bounded_shutdown_timeout
from .listener_pause import paused_listeners
from .loop_host import run as run_hosted
from .nats_errors import publish_mapped
from .outputs import OutputBindings
from .schedules import ScheduledPublishing
from .service_config import ServiceConfig
from .validation import serialize_payload, wire_models
from .validation_extension import ValidationExtension

__all__ = [
    "CliffracerService",
    "Container",
    "BrokerConnectionState",
    "DispatchOutcome",
    "redact_nats_url",
    "_CLOSED_STOP_TIMEOUT",
]


# A best-effort publish is given up after this. The ceiling it replaces is
# nats-py's own: `nc.jetstream()` takes the context default of five seconds,
# and `JetStreamContext.publish` falls back to it when given no timeout, so a
# broker that never acknowledges holds the caller for that long.
BEST_EFFORT_PUBLISH_SECONDS = 2.0

# How many distinct subjects the unconfirmed-publish tally names. A service
# publishing per entity would otherwise grow one key, and one distinct warning,
# for every entity it touched during an outage; past this, subjects are counted
# together under OTHER_SUBJECT so the tally stays bounded and still says how
# many were given up on.
UNCONFIRMED_SUBJECTS = 32
OTHER_SUBJECT = "other"


def event_envelope(
    source_service: str, kwargs: dict[str, Any], *, envelope: bool = True
) -> tuple[dict[str, Any], Any, str]:
    """An event's payload, its domain data and its correlation id, from `publish_event`'s kwargs.

    `kwargs` loses its `correlation_id`. With `envelope`, the payload is the event envelope: the
    domain data (a lone `data` dict, else the keyword arguments), a timestamp, the source service
    and the correlation id; without it, the keyword arguments and the correlation id.
    """
    cid = CorrelationContext.get_or_create_id(kwargs.pop("correlation_id", None))
    domain_data = (
        kwargs["data"]
        if (len(kwargs) == 1 and "data" in kwargs and isinstance(kwargs["data"], dict))
        else kwargs
    )
    if envelope:
        payload: dict[str, Any] = {
            "data": domain_data,
            "timestamp": datetime.now(UTC).isoformat(),
            "source_service": source_service,
            "correlation_id": cid,
        }
    else:
        payload = dict(kwargs)
        payload["correlation_id"] = cid
    return payload, domain_data, cid


class CliffracerService:
    """Application runtime façade executing over NATS.

    Invariants:
    - Instantiates and owns a Container coordinating isolated runtime components.
    - Holds no inbound dispatch callbacks and no connection management: those live in the
      container.
    - Makes the outbound sends itself (`call_rpc`, `stream_rpc`, `call_async`,
      `call_rpc_no_wait`, `publish_event`, `broadcast_message`): the request or publish on the
      connection, and the stream-coverage check before a JetStream publish. Each send runs
      inside the send hooks the container's outbound dispatcher builds. `call_rpc`'s and
      `stream_rpc`'s bodies are in `core/rpc_calls.py`.
    - Subclasses declare handlers via decorators (@rpc, @listener, @timer).
    """

    _HANDLER_MARKER_PREFIX = "_cliffracer_"

    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.logger = logger.bind(
            service=self.config.name,
            service_type=self.__class__.__name__,
        )

        # Construct internal container coordinator
        self._container = Container(self, config)
        #: Publishing a message at a later time: `publish_at`, `publish_in` and `cancel`.
        self.schedules = ScheduledPublishing(self)

        # Best-effort events given up on, by (subject, reason), since start.
        self._unconfirmed_events: dict[tuple[str, str], int] = {}
        self._output_bindings: OutputBindings | None = None

        # What the readiness check asks the broker, bounded and cached.
        self._broker_probe = BrokerProbe(
            config.broker_probe_timeout, config.broker_probe_cache, service=config.name
        )

        # Dependencies registered on this service
        self._dependencies: list[Dependency] = []
        self._register_dependencies()

        # Collect and bind extensions
        self._collect_extensions()

        # Built-in HTTP health probe listener
        self.health_listener = HealthListener(self, config.health_host, config.health_port)
        if not config.health_listener:
            self.health_listener.disable("health_listener=False")

    @property
    def container(self) -> Container:
        """Internal container coordinator."""
        return self._container

    @property
    def output_bindings(self) -> OutputBindings | None:
        """Accepted typed output routes and public producer metadata."""
        return self._output_bindings

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

        run_hosted(
            _run(),
            teardown_timeout=lambda: bounded_shutdown_timeout(
                self.config.shutdown_timeout, self.logger, "Joining the tasks left at teardown"
            ),
        )

    async def on_startup(self) -> None:
        """Hook executed during start() after NATS connection and stream setup.

        `on_shutdown` pairs with an `on_startup` that RETURNED. If this hook raises or is
        cancelled, no `on_shutdown` runs: release whatever it had built before it raises.
        """

    async def on_shutdown(self) -> None:
        """Hook executed during stop() before extension teardown and disconnect.

        Runs only for a startup whose `on_startup` returned, including the teardown of a
        startup that failed in a later step.
        """

    def _discover_handlers(self) -> None:
        """Trigger handler discovery across the service instance."""
        self.container.discover_handlers()

    # ---- Extension Registration ----

    def _collect_extensions(self) -> None:
        # Core's two extensions go around the ones the service declares. `_correlation` is first,
        # so every declared extension sees a correlation id. `_validation` is last, so a gate the
        # service declares (authentication, a rate limit) refuses a message before any schema
        # diagnostic is produced for it, and the service's own validators never run on input a gate
        # has turned away. `_bind_extension` keeps `_validation` last for an extension added later
        # with `add_extension`.
        self.container._bind_extension(CorrelationExtension(), "_correlation")

        declared: dict[str, Extension] = {}
        for klass in reversed(type(self).__mro__):
            for attr, value in vars(klass).items():
                if isinstance(value, Extension):
                    declared[attr] = value
        for attr, value in declared.items():
            self.container._bind_extension(value, attr)

        self.container._bind_extension(ValidationExtension(), "_validation")

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
        # Built before the old one is dropped: a refused declaration (a timeout that
        # could never run the probe) must leave what was registered in place.
        declared = Dependency(name=name, probe=probe, timeout=timeout, detail=detail)
        self._dependencies = [dep for dep in self._dependencies if dep.name != name]
        self._dependencies.append(declared)
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
        """Perform readiness health check evaluating broker and dependency probes.

        A stopped service runs no probe: it is `stopped` whatever its dependencies say, and its
        downstreams are left alone, so the answer is immediate and has no `dependencies` block.
        `connecting` and `disconnected` still run them, since "the broker is down and so is the
        database" is a diagnosis.
        """
        is_connected = self.is_broker_connected
        asking = is_connected and self.nc is not None
        if self._running:
            (dependencies, dependencies_error), probe = await asyncio.gather(
                self._check_dependencies(), self._ask_the_broker(asking)
            )
        else:
            dependencies, dependencies_error, probe = {}, None, None
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
        elif probe is not None and not probe.ok:
            status = "disconnected"
        elif broken or dependencies_error:
            status = "unhealthy"
        else:
            status = "healthy"

        health: dict[str, Any] = {
            "service": self.config.name,
            "status": status,
            "timestamp": datetime.now(UTC).isoformat(),
            "nats_connected": (is_connected and (probe is None or probe.ok)) if self.nc else None,
            "nats_rtt_ms": probe.rtt_ms if probe is not None else None,
            "broker_state": self.broker_state.value,
            "features": self.container.registry.feature_counts(),
            "dead_letters_lost": self.container.dead_letters_lost,
            **health_details(self.container.dispatcher.limits, self.container.registry),
        }

        if dependencies:
            health["dependencies"] = dependencies
            if broken:
                health["unhealthy_dependencies"] = broken
        health.update(paused_listeners(self.container.listener_pauses))
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
                    # An extension's own words on an unauthenticated endpoint.
                    health[ext.name] = {
                        "error": exception_text(
                            exc, self.config, generic="health details unavailable"
                        )
                    }
                    continue
                if contribution is not None:
                    health[ext.name] = contribution
        except Exception as exc:  # noqa: BLE001
            # The loop itself failed rather than one extension's details -- an
            # extension whose `name` raises, say, which the inner handler above
            # never sees. Same endpoint, same absence of authentication, so the
            # same gate.
            self.logger.warning(f"Health details unavailable: {exc}")
            health["details_error"] = exception_text(
                exc, self.config, generic="health details unavailable"
            )

        return health

    async def _ask_the_broker(self, asking: bool) -> ProbeResult | None:
        """One round trip to the broker for the readiness check, or None when it is not asked."""
        if not asking or not self._broker_probe.enabled:
            return None
        return await self._broker_probe.check(self.nc)

    async def _check_dependencies(self) -> tuple[dict[str, dict[str, Any]], str | None]:
        try:
            return await check_dependencies(self._dependencies, self.config), None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"Dependency checks unavailable: {exc}")
            return {}, exception_text(exc, self.config, generic="dependency checks unavailable")

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
                "events": HandlerDiscovery.with_namespace(
                    self.config, f"{self.config.name}.events.*"
                ),
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

    def _require_connection(self, subject: str, *, call: bool) -> nats.NATS:
        """The broker connection a send needs, or the named error a send without one raises.

        Checked before the send hooks run, so a service that is not connected starts no span and
        stamps no header for a message that cannot leave. A call raises what the standalone client
        raises for a connection that is not there, `RpcConnectionError`; a publish raises
        `ServiceLifecycleError`, as a JetStream publish already does when its context is missing.
        """
        nc = self.nc
        if nc is not None:
            return nc
        message = (
            f"service {self.config.name!r} is not connected to the broker, so nothing is sent to "
            f"{subject!r}: start the service, or connect it, first"
        )
        if call:
            raise RpcConnectionError(message)
        raise ServiceLifecycleError(message)

    async def call_rpc(
        self, service: str, method: str, /, *, namespace: str | None = None, **kwargs: Any
    ) -> Any:
        """Call an RPC method on another service, awaiting response.

        The call waits `request_timeout`, or less when it is made inside a handler whose own
        request has less left, and sends what it waits in `Cliffracer-Timeout-Ms`, so the
        service it calls stops when this caller does. A call made when that has run out raises
        `RpcTimeoutError` and is not sent.
        """
        return await rpc_calls.call_rpc(self, service, method, namespace=namespace, **kwargs)

    def stream_rpc(
        self, service: str, method: str, /, *, namespace: str | None = None, **kwargs: Any
    ) -> AsyncGenerator[Any]:
        """Call an RPC method that streams its reply, and yield each item as it arrives.

        The whole stream is bounded by `request_timeout`, or less when it is opened inside a
        handler whose own request has less left, and sends that in `Cliffracer-Timeout-Ms`. An
        error the stream ends with is raised after the items before it, carrying `items`.
        Leaving the loop early unsubscribes the reply inbox, and the service stops soon after.

        The send hooks (`before_call`, `after_call`, kind `stream_rpc`) run once, around opening
        the stream: subscribing its inbox and sending the request. A hook that counts calls
        counts a stream once, whatever the number of its items.
        """
        return rpc_calls.stream_rpc(self, service, method, namespace=namespace, **kwargs)

    async def call_async(
        self, service: str, method: str, /, *, namespace: str | None = None, **kwargs: Any
    ) -> Any:
        """Call an RPC method asynchronously without awaiting a reply."""
        subject = HandlerDiscovery.outbound_subject(
            self.config, service, "async", method, namespace=namespace
        )
        reason = _unusable_subject_reason(subject)
        if reason is not None:
            raise ValueError(f"Invalid async subject {subject!r}: {reason}")
        nc = self._require_connection(subject, call=True)

        kwargs["correlation_id"] = CorrelationContext.get_or_create_id(kwargs.get("correlation_id"))

        self.logger.info(
            f"Calling async {service}.{method} with correlation_id: {kwargs['correlation_id']}"
        )

        request_data, content_type = serialize_payload(
            wire_models(kwargs), format=self.config.serialization_format
        )
        ctx = self.container.dispatcher._send_context(
            "call_async", subject, kwargs, kwargs["correlation_id"]
        )
        ctx.headers["Content-Type"] = content_type

        async def _send() -> None:
            await publish_mapped(
                nc.publish(subject, request_data, headers=dict(ctx.headers)), subject
            )

        return await self.container.dispatcher._run_send_hooks(ctx, _send)

    async def call_rpc_no_wait(
        self, service: str, method: str, /, *, namespace: str | None = None, **kwargs: Any
    ) -> Any:
        """Call an RPC method without waiting for response."""
        subject = HandlerDiscovery.outbound_subject(
            self.config, service, "rpc", method, namespace=namespace
        )
        reason = _unusable_subject_reason(subject)
        if reason is not None:
            raise ValueError(f"Invalid RPC subject {subject!r}: {reason}")
        nc = self._require_connection(subject, call=True)

        kwargs["correlation_id"] = CorrelationContext.get_or_create_id(kwargs.get("correlation_id"))

        request_data, content_type = serialize_payload(
            wire_models(kwargs), format=self.config.serialization_format
        )
        ctx = self.container.dispatcher._send_context(
            "call_rpc_no_wait", subject, kwargs, kwargs["correlation_id"]
        )
        ctx.headers["Content-Type"] = content_type

        async def _send() -> None:
            await publish_mapped(
                nc.publish(subject, request_data, headers=dict(ctx.headers)), subject
            )

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
        payload, domain_data, cid = event_envelope(self.config.name, kwargs, envelope=envelope)
        self.logger.info(f"Publishing event {subject} with correlation_id: {cid}")

        full_subject = HandlerDiscovery.with_namespace(self.config, subject)

        return await self._send_event(
            full_subject,
            payload,
            domain_data,
            cid,
            idempotency_key=idempotency_key,
            idempotent=idempotent,
        )

    async def _publish_bound_output(
        self,
        subject: str,
        data: dict[str, Any],
        metadata: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        """Send a validated typed event on its already scoped, accepted route."""
        cid = CorrelationContext.get_or_create_id()
        payload = {
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
            "source_service": self.config.name,
            "correlation_id": cid,
            "output": metadata,
        }
        return await self._send_event(
            subject,
            payload,
            {"data": data, "output": metadata},
            cid,
            idempotency_key=idempotency_key,
        )

    async def _send_event(
        self,
        full_subject: str,
        payload: dict[str, Any],
        domain_data: Any,
        cid: str,
        *,
        idempotency_key: str | None = None,
        idempotent: bool | None = None,
        schedule: tuple[str, dict[str, str]] | None = None,
    ) -> Any:
        """Serialize an event before its send hooks and preserve publishing semantics.

        With `schedule`, the event is published to the schedule subject it names, carrying its
        schedule headers, for the broker to write onto `full_subject` when it is due. The send
        hooks are given `full_subject`, so a hook that counts by subject counts a scheduled event
        under its target, at the moment it is scheduled.
        """
        self._require_connection(full_subject, call=False)
        sent_to = full_subject if schedule is None else schedule[0]

        # Check explicit idempotency_key, ambient IdempotencyContext, or config / parameter
        key_candidate = idempotency_key or IdempotencyContext.get()
        if key_candidate is None and (
            idempotent or getattr(self.config, "idempotent_publishing", False)
        ):
            key_candidate = compute_payload_hash(domain_data)

        # Serialised before the hooks, as every send path is: a hook sees the
        # payload but cannot change the message, so the bytes sent are the ones
        # the key above was computed from.
        event_data, content_type = serialize_payload(
            wire_models(payload), format=self.config.serialization_format
        )
        ctx = self.container.dispatcher._send_context("publish_event", full_subject, payload, cid)

        if key_candidate is not None:
            # A key the caller passed is used exactly as given: it carries no ordinal and does not
            # advance the call's count, because the caller who names a key per publish is naming
            # the message, and an id that depended on how many publishes came before would make
            # that naming depend on publish order. The ordinal belongs to a key this call derived
            # (the ambient key, or the payload hash): it is this message's place among the
            # decorated call's own, so two publishes to one subject under one ambient key do not
            # collide. It is `None` outside a decorated call, where the caller owns the key.
            ctx.headers["Nats-Msg-Id"] = format_nats_msg_id(
                sent_to,
                str(key_candidate),
                sequence=None if idempotency_key else IdempotencyContext.next_sequence(),
            )

        if schedule is not None:
            ctx.headers.update(schedule[1])
        return await self.container.dispatcher._run_send_hooks(
            ctx,
            lambda: self._publish_serialized(sent_to, event_data, content_type, dict(ctx.headers)),
        )

    def publish_event_nowait(
        self,
        subject: str,
        *,
        timeout: float = BEST_EFFORT_PUBLISH_SECONDS,
        **kwargs: Any,
    ) -> asyncio.Task[Any]:
        """Publish an event beside the caller instead of inside it.

        **Best effort.** The caller returns without waiting for the broker, so
        a publish that is refused, that times out, or that is never
        acknowledged does not hold a reply, a handler or a shutdown. Nothing
        is retried and nothing is queued: a consumer that must not miss a
        message needs `publish_event`, or a record it can read instead.

        What is kept: the task is supervised, so the service drains it at stop
        before the connection is closed, and every message given up on is
        counted in `unconfirmed_events` and logged with its subject and
        reason. That tally names at most `UNCONFIRMED_SUBJECTS` subjects and
        counts the rest together under `"other"`, so a service publishing per
        entity cannot grow it without bound.

        The correlation and idempotency context are the caller's, captured
        here rather than read when the send happens, because a task carries a
        copy of the context that created it.
        """
        return self.container._spawn_supervised_task(
            self._publish_event_best_effort(subject, timeout=timeout, **kwargs),
            name=f"publish_event_nowait:{subject}",
        )

    async def _publish_event_best_effort(
        self, subject: str, *, timeout: float, **kwargs: Any
    ) -> None:
        """Publish within `timeout`, counting and naming whatever stops it."""
        try:
            async with asyncio.timeout(timeout):
                await self.publish_event(subject, **kwargs)
        except TimeoutError:
            self._not_confirmed(subject, "timeout")
        except asyncio.CancelledError:
            # Stopping is not dropping: the drain decides what happens next.
            raise
        except Exception as error:
            self._not_confirmed(subject, type(error).__name__)

    def _not_confirmed(self, subject: str, reason: str) -> None:
        """Count a publish this service stopped waiting for, by subject.

        A subject the tally does not already name is counted under
        `OTHER_SUBJECT` once it names `UNCONFIRMED_SUBJECTS` of them, so a
        burst of per-entity subjects cannot grow it; a subject already named
        keeps its own key, so a steady publisher does not lose its count.
        """
        named = {shown for shown, _ in self._unconfirmed_events}
        if subject not in named and len(named) >= UNCONFIRMED_SUBJECTS:
            subject = OTHER_SUBJECT
        count = self._unconfirmed_events.get((subject, reason), 0) + 1
        self._unconfirmed_events[(subject, reason)] = count
        self.logger.warning(f"Publish not confirmed on {subject}: {reason} ({count} since start)")

    @property
    def unconfirmed_events(self) -> dict[tuple[str, str], int]:
        """Publishes this service stopped waiting for, by (subject, reason).

        Not "dropped": `timeout` says the wait was given up, not that the
        broker refused the message -- JetStream may have stored it and
        answered late. What the service knows is that it did not see the
        answer.
        """
        return dict(self._unconfirmed_events)

    async def _publish_serialized(
        self, full_subject: str, event_data: bytes, content_type: str, headers: dict[str, Any]
    ) -> Any:
        """Publish bytes serialised before the send hooks, without running them."""
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = content_type

        if self.config.jetstream_enabled and self.js is None:
            # Declared, but not connected: falling through would publish the event
            # unacknowledged on core NATS, to a subject no stream was checked against,
            # in exactly the configuration that asked for the opposite.
            raise ServiceLifecycleError(
                f"jetstream_enabled is on but service {self.config.name!r} has no JetStream "
                f"context (it is not connected), so {full_subject!r} is not published: the "
                f"alternative is an unacknowledged core NATS publish to a subject no "
                f"declared stream covers."
            )

        if self.container._jetstream_active:
            # The effective list, because full_subject already carries the
            # prefix: comparing it against the unprefixed declaration would
            # refuse every publish a prefixed service makes.
            declared = self.config.effective_jetstream_streams
            if not subject_covered_by(declared, full_subject):
                claims = [s for spec in declared for s in spec.subjects]
                raise StreamDeclarationError(
                    f"jetstream_enabled is on, but no declared stream covers "
                    f"{full_subject!r}. Declared claims: {claims}. Every subject a "
                    f"service publishes needs a stream, including ones nothing consumes."
                )
            assert self.js is not None
            ack = await publish_mapped(
                self.js.publish(full_subject, event_data, headers=headers), full_subject
            )
            if getattr(ack, "duplicate", False):
                self.logger.info(
                    f"JetStream detected duplicate publish on {full_subject} with Nats-Msg-Id: "
                    f"{headers.get('Nats-Msg-Id')}"
                )
            return ack

        nc = self._require_connection(full_subject, call=False)
        await publish_mapped(nc.publish(full_subject, event_data, headers=headers), full_subject)

    async def broadcast_message(self, subject: str, **kwargs: Any) -> None:
        """Publish a broadcast event, namespaced as a broadcast listener subscribes."""
        cid = CorrelationContext.get_or_create_id(kwargs.pop("correlation_id", None))
        message: dict[str, Any] = {
            "data": kwargs,
            "timestamp": datetime.now(UTC).isoformat(),
            "source_service": self.config.name,
            "correlation_id": cid,
        }

        full_subject = HandlerDiscovery.with_namespace(self.config, subject)
        self._require_connection(full_subject, call=False)

        # Normalised once, before the hooks: the wire carries this value, so a
        # hook cannot change it and a one-shot iterable reaches the wire with its
        # values.
        data = pydantic_core.to_jsonable_python(wire_models(kwargs))
        event_data, content_type = serialize_payload(
            {**message, "data": data}, format=self.config.serialization_format
        )

        ctx = self.container.dispatcher._send_context("broadcast", full_subject, message, str(cid))

        async def _send() -> None:
            await self._publish_serialized(
                full_subject, event_data, content_type, dict(ctx.headers)
            )

        await self.container.dispatcher._run_send_hooks(ctx, _send)
        self.logger.debug(f"Broadcasted message: {subject}")
