"""
Consolidated Cliffracer Service Architecture

This module provides a clean, consolidated service architecture that replaces
the previous BaseNATSService/ExtendedNATSService hierarchy with a composable
mixin-based approach.
"""

import asyncio
import inspect
import json
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import nats
from loguru import logger
from nats.errors import TimeoutError
from nats.js import JetStreamContext

from .correlation import CorrelationContext
from .mixins import BroadcastMixin, HTTPMixin, PerformanceMixin, ValidationMixin, WebSocketMixin
from .service_config import ServiceConfig


class CliffracerService:
    """
    Core Cliffracer service with NATS messaging capabilities.

    This is the base service class that provides essential NATS functionality.
    Additional features can be added via mixins.
    """

    def __init__(self, config: ServiceConfig):
        self.config = config
        self.nc: nats.NATS | None = None
        self.js: JetStreamContext | None = None
        self._subscriptions: set[asyncio.Task] = set()
        self._running = False

        # Handler registries
        self._rpc_handlers: dict[str, Callable] = {}
        self._event_handlers: dict[str, Callable] = {}
        self._timers: list[Any] = []  # Timer instances

        # Optional features
        self._backdoor_server: Any = None

        # Discover and register handlers automatically
        self._handlers_discovered = False

    def _discover_handlers(self):
        """Discover and register decorated methods"""
        if self._handlers_discovered:
            return
        self._handlers_discovered = True

        for name in dir(self):
            if name.startswith("_"):
                continue

            method = getattr(self, name)

            # Discover RPC handlers
            if hasattr(method, "_cliffracer_rpc"):
                self._rpc_handlers[name] = method
                logger.debug(f"Discovered RPC handler: {name}")

            # Discover event handlers
            if hasattr(method, "_cliffracer_events"):
                for pattern in method._cliffracer_events:
                    self._event_handlers[pattern] = method
                    logger.debug(f"Discovered event handler: {pattern}")

            # Discover timers
            if hasattr(method, "_cliffracer_timers"):
                for timer_instance in method._cliffracer_timers:
                    self._timers.append(timer_instance)
                    logger.debug(f"Discovered timer: {timer_instance.method_name}")

            # Discover validated RPC handlers
            if hasattr(method, "_cliffracer_validated_rpc"):
                schema = method._cliffracer_validated_rpc
                if hasattr(self, "register_validated_rpc"):
                    self.register_validated_rpc(name, method, schema)
                    logger.debug(f"Discovered validated RPC handler: {name}")
                else:
                    # Fall back to regular RPC if no validation mixin
                    self._rpc_handlers[name] = method
                    logger.debug(f"Discovered validated RPC handler (no validation): {name}")

            # Discover broadcast handlers
            if hasattr(method, "_cliffracer_broadcast"):
                pattern = method._cliffracer_broadcast
                if hasattr(self, "register_broadcast_handler"):
                    self.register_broadcast_handler(pattern, method)
                    logger.debug(f"Discovered broadcast handler: {pattern}")
                else:
                    # Fall back to event handler if no broadcast mixin
                    self._event_handlers[pattern] = method
                    logger.debug(f"Discovered broadcast handler (no broadcast): {pattern}")

            # Discover WebSocket handlers
            if hasattr(method, "_cliffracer_websocket"):
                path = method._cliffracer_websocket
                if hasattr(self, "register_websocket_handler"):
                    self.register_websocket_handler(path, method)
                    logger.debug(f"Discovered WebSocket handler: {path}")
                else:
                    logger.warning(
                        f"WebSocket handler {name} found but no WebSocket mixin available"
                    )

    async def connect(self):
        """Connect to NATS server"""
        self.nc = await nats.connect(
            self.config.nats_url,
            name=self.config.name,
            max_reconnect_attempts=self.config.max_reconnect_attempts,
            reconnect_time_wait=self.config.reconnect_time_wait,
            error_cb=self._error_callback,
            disconnected_cb=self._disconnected_callback,
            reconnected_cb=self._reconnected_callback,
            closed_cb=self._closed_callback,
        )

        if self.config.jetstream_enabled:
            self.js = self.nc.jetstream()

        logger.info(f"Service '{self.config.name}' connected to NATS at {self.config.nats_url}")

        # Start backdoor server if enabled
        await self._start_backdoor()

    async def disconnect(self):
        """Disconnect from NATS server"""
        await self._stop_backdoor()

        if self.nc and not self.nc.is_closed:
            await self.nc.drain()
            await self.nc.close()

    async def start(self):
        """Start the service and all its features"""
        # Ensure handlers are discovered after all mixins are initialized
        self._discover_handlers()

        await self.connect()
        self._running = True

        # Start performance features if available
        if hasattr(self, "start_performance_features"):
            await self.start_performance_features()

        # Start HTTP server if available
        if hasattr(self, "start_http"):
            await self.start_http()

        # Start timers
        await self._start_timers()

        # Set up NATS subscriptions
        await self._setup_subscriptions()

        feature_counts = self._get_feature_counts()
        logger.info(
            f"Service '{self.config.name}' started with "
            f"{feature_counts['rpc']} RPC handlers, "
            f"{feature_counts['events']} event handlers, "
            f"{feature_counts['timers']} timers"
        )

    async def stop(self):
        """Stop the service and cleanup all resources"""
        self._running = False

        # Stop timers
        await self._stop_timers()

        # Stop HTTP server if available
        if hasattr(self, "stop_http"):
            await self.stop_http()

        # Stop performance features if available
        if hasattr(self, "stop_performance_features"):
            await self.stop_performance_features()

        # Cancel all subscriptions
        for task in self._subscriptions:
            task.cancel()

        await asyncio.gather(*self._subscriptions, return_exceptions=True)
        self._subscriptions.clear()

        await self.disconnect()
        logger.info(f"Service '{self.config.name}' stopped")

    async def _setup_subscriptions(self):
        """Set up NATS subscriptions"""
        # Subscribe to RPC subjects
        rpc_subject = f"{self.config.name}.rpc.*"
        sub = await self.nc.subscribe(rpc_subject, cb=self._handle_rpc_request)
        self._subscriptions.add(asyncio.create_task(self._subscription_handler(sub)))

        # Subscribe to async subjects
        async_subject = f"{self.config.name}.async.*"
        sub = await self.nc.subscribe(async_subject, cb=self._handle_async_request)
        self._subscriptions.add(asyncio.create_task(self._subscription_handler(sub)))

        # Subscribe to event subjects
        for pattern in self._event_handlers:
            sub = await self.nc.subscribe(pattern, cb=self._handle_event)
            self._subscriptions.add(asyncio.create_task(self._subscription_handler(sub)))

    async def _handle_rpc_request(self, msg):
        """Handle incoming RPC requests"""
        return await self._handle_rpc_request_base(msg)

    async def _handle_rpc_request_base(self, msg):
        """Base RPC request handling"""

        subject = msg.subject
        handler_name = subject.split(".")[-1]

        if handler_name not in self._rpc_handlers:
            error_response = {
                "error": f"Unknown method: {handler_name}",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            await msg.respond(json.dumps(error_response).encode())
            return

        handler = self._rpc_handlers[handler_name]

        try:
            # Parse request data
            data = json.loads(msg.data.decode()) if msg.data else {}

            # Extract correlation ID from request
            correlation_id = data.get("correlation_id")
            if not correlation_id and hasattr(msg, "headers") and msg.headers:
                correlation_id = msg.headers.get("correlation_id")
            correlation_id = CorrelationContext.get_or_create_id(correlation_id)
            CorrelationContext.set(correlation_id)

            # Log with correlation ID
            logger.info(f"RPC request {handler_name} with correlation_id: {correlation_id}")

            # Inject correlation ID into handler kwargs
            data["correlation_id"] = correlation_id

            # Call handler - remove correlation_id if handler doesn't accept it
            sig = inspect.signature(handler)
            if "correlation_id" not in sig.parameters:
                data.pop("correlation_id", None)

            if inspect.iscoroutinefunction(handler):
                result = await handler(**data)
            else:
                result = handler(**data)

            # Send response with correlation ID
            response = {
                "result": result,
                "timestamp": datetime.now(UTC).isoformat(),
                "correlation_id": correlation_id,
            }
            await msg.respond(json.dumps(response).encode())

        except Exception as e:
            correlation_id = CorrelationContext.get()
            logger.error(
                f"Error handling RPC request {handler_name} (correlation_id: {correlation_id}): {e}"
            )
            error_response = {
                "error": str(e),
                "traceback": traceback.format_exc(),
                "timestamp": datetime.now(UTC).isoformat(),
                "correlation_id": correlation_id,
            }
            await msg.respond(json.dumps(error_response).encode())

    async def _handle_async_request(self, msg):
        """Handle incoming async requests (fire-and-forget)"""
        subject = msg.subject
        handler_name = subject.split(".")[-1]

        if handler_name not in self._rpc_handlers:
            logger.warning(f"Unknown async method: {handler_name}")
            return

        handler = self._rpc_handlers[handler_name]

        try:
            data = json.loads(msg.data.decode()) if msg.data else {}

            # Extract and set correlation ID
            correlation_id = data.get("correlation_id")
            if not correlation_id and hasattr(msg, "headers") and msg.headers:
                correlation_id = msg.headers.get("correlation_id")
            correlation_id = CorrelationContext.get_or_create_id(correlation_id)
            CorrelationContext.set(correlation_id)

            logger.info(f"Async request {handler_name} with correlation_id: {correlation_id}")
            data["correlation_id"] = correlation_id

            # Remove correlation_id if handler doesn't accept it
            sig = inspect.signature(handler)
            if "correlation_id" not in sig.parameters:
                data.pop("correlation_id", None)

            if inspect.iscoroutinefunction(handler):
                await handler(**data)
            else:
                handler(**data)

        except Exception as e:
            correlation_id = CorrelationContext.get()
            logger.error(
                f"Error handling async request {handler_name} (correlation_id: {correlation_id}): {e}"
            )

    async def _handle_event(self, msg):
        """Handle incoming events"""
        subject = msg.subject

        # Find matching handlers
        matching_handlers = []
        for pattern, handler in self._event_handlers.items():
            if self._subject_matches(pattern, subject):
                matching_handlers.append(handler)

        if not matching_handlers:
            return

        for handler in matching_handlers:
            try:
                data = json.loads(msg.data.decode()) if msg.data else {}

                # Extract and set correlation ID for events
                correlation_id = data.get("correlation_id")
                if not correlation_id and hasattr(msg, "headers") and msg.headers:
                    correlation_id = msg.headers.get("correlation_id")
                correlation_id = CorrelationContext.get_or_create_id(correlation_id)
                CorrelationContext.set(correlation_id)

                logger.info(f"Event {subject} with correlation_id: {correlation_id}")
                data["correlation_id"] = correlation_id

                # Check if handler accepts subject parameter
                sig = inspect.signature(handler)
                if "subject" not in sig.parameters:
                    # Don't pass subject if handler doesn't accept it
                    if "correlation_id" not in sig.parameters:
                        data.pop("correlation_id", None)

                    if inspect.iscoroutinefunction(handler):
                        await handler(**data)
                    else:
                        handler(**data)
                else:
                    # Pass subject if handler accepts it
                    if "correlation_id" not in sig.parameters:
                        data.pop("correlation_id", None)

                    if inspect.iscoroutinefunction(handler):
                        await handler(subject=subject, **data)
                    else:
                        handler(subject=subject, **data)

            except Exception as e:
                correlation_id = CorrelationContext.get()
                logger.error(
                    f"Error handling event {subject} (correlation_id: {correlation_id}): {e}"
                )

    def _subject_matches(self, pattern: str, subject: str) -> bool:
        """Check if subject matches pattern (supports wildcards)"""
        pattern_parts = pattern.split(".")
        subject_parts = subject.split(".")

        if len(pattern_parts) != len(subject_parts) and ">" not in pattern:
            return False

        for p, s in zip(pattern_parts, subject_parts, strict=False):
            if p == ">":
                return True
            elif p == "*":
                continue
            elif p != s:
                return False

        return True

    async def _subscription_handler(self, sub):
        """Handle subscription lifecycle"""
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            await sub.unsubscribe()

    # Timer management
    async def _start_timers(self):
        """Start all discovered timers"""
        for timer_instance in self._timers:
            await timer_instance.start(self)

    async def _stop_timers(self):
        """Stop all running timers"""
        for timer_instance in self._timers:
            await timer_instance.stop()

    def get_timer_stats(self) -> dict[str, Any]:
        """Get statistics for all timers"""
        return {
            "timer_count": len(self._timers),
            "timers": [timer.get_stats() for timer in self._timers],
        }

    # Backdoor server management
    async def _start_backdoor(self):
        """Start backdoor server if enabled"""
        if not self.config.backdoor_enabled:
            return

        try:
            from ..debug.backdoor import BackdoorServer

            self._backdoor_server = BackdoorServer(
                service_instance=self,
                port=self.config.backdoor_port,
                password=self.config.backdoor_password,
            )
            self._backdoor_server.start()
            logger.info(f"Backdoor server started on localhost:{self._backdoor_server.port}")
        except ImportError:
            logger.warning("Backdoor functionality not available")
        except Exception as e:
            logger.error(f"Failed to start backdoor server: {e}")

    async def _stop_backdoor(self):
        """Stop backdoor server"""
        if self._backdoor_server:
            await self._backdoor_server.stop()
            self._backdoor_server = None

    # Callback methods
    async def _error_callback(self, e):
        logger.error(f"NATS error: {e}")

    async def _disconnected_callback(self):
        logger.warning(f"Service '{self.config.name}' disconnected from NATS")

    async def _reconnected_callback(self):
        logger.info(f"Service '{self.config.name}' reconnected to NATS")

    async def _closed_callback(self):
        logger.info(f"Service '{self.config.name}' connection closed")

    # Utility methods
    def _get_feature_counts(self) -> dict[str, int]:
        """Get counts of various features"""
        return {
            "rpc": len(self._rpc_handlers),
            "events": len(self._event_handlers),
            "timers": len(self._timers),
            "websockets": len(getattr(self, "_websocket_handlers", {})),
            "broadcasts": len(getattr(self, "_broadcast_handlers", {})),
        }

    # RPC client methods
    async def call_rpc(self, service: str, method: str, **kwargs) -> Any:
        """Call an RPC method on another service"""
        subject = f"{service}.rpc.{method}"

        # Inject correlation ID if not present
        if "correlation_id" not in kwargs:
            kwargs["correlation_id"] = (
                CorrelationContext.get() or CorrelationContext.get_or_create_id()
            )

        correlation_id = kwargs["correlation_id"]
        logger.info(f"Calling RPC {service}.{method} with correlation_id: {correlation_id}")

        request_data = json.dumps(kwargs).encode()

        try:
            response = await self.nc.request(
                subject, request_data, timeout=self.config.request_timeout
            )

            response_data = json.loads(response.data.decode())

            if "error" in response_data:
                raise Exception(f"RPC Error: {response_data['error']}")

            return response_data.get("result")

        except TimeoutError as e:
            logger.error(
                f"RPC timeout calling {service}.{method} (correlation_id: {correlation_id})"
            )
            raise Exception(f"RPC timeout calling {service}.{method}") from e

    async def call_async(self, service: str, method: str, **kwargs):
        """Call an RPC method asynchronously (fire-and-forget)"""
        subject = f"{service}.async.{method}"

        # Inject correlation ID if not present
        if "correlation_id" not in kwargs:
            kwargs["correlation_id"] = (
                CorrelationContext.get() or CorrelationContext.get_or_create_id()
            )

        logger.info(
            f"Calling async {service}.{method} with correlation_id: {kwargs['correlation_id']}"
        )

        request_data = json.dumps(kwargs).encode()
        await self.nc.publish(subject, request_data)

    async def call_rpc_no_wait(self, service: str, method: str, **kwargs):
        """
        Call an RPC method without waiting for response (fire-and-forget)

        Args:
            service: Target service name
            method: RPC method name
            **kwargs: Method arguments
        """
        subject = f"{service}.rpc.{method}"

        # Inject correlation ID if not present
        if "correlation_id" not in kwargs:
            kwargs["correlation_id"] = (
                CorrelationContext.get() or CorrelationContext.get_or_create_id()
            )

        request_data = json.dumps(kwargs).encode()
        await self.nc.publish(subject, request_data)

    async def publish_event(self, subject: str, **kwargs):
        """Publish an event"""
        # Inject correlation ID if not present
        if "correlation_id" not in kwargs:
            kwargs["correlation_id"] = (
                CorrelationContext.get() or CorrelationContext.get_or_create_id()
            )

        logger.info(f"Publishing event {subject} with correlation_id: {kwargs['correlation_id']}")

        event_data = json.dumps(kwargs).encode()
        await self.nc.publish(subject, event_data)

    async def health_check(self) -> dict[str, Any]:
        """Perform health check"""
        health = {
            "service": self.config.name,
            "status": "healthy" if self._running else "stopped",
            "timestamp": datetime.now(UTC).isoformat(),
            "nats_connected": self.nc and not self.nc.is_closed,
            "features": self._get_feature_counts(),
        }

        # Add WebSocket stats if available
        if hasattr(self, "get_websocket_stats"):
            health["websockets"] = self.get_websocket_stats()

        # Add performance metrics if available
        if hasattr(self, "get_performance_metrics"):
            try:
                health["performance"] = self.get_performance_metrics()
            except Exception:
                health["performance"] = {"error": "Metrics unavailable"}

        return health

    def get_service_info(self) -> dict[str, Any]:
        """Get service metadata"""
        info = {
            "name": self.config.name,
            "version": self.config.version,
            "rpc_methods": list(self._rpc_handlers.keys()),
            "event_patterns": list(self._event_handlers.keys()),
            "timer_methods": [timer.method_name for timer in self._timers],
            "subjects": {
                "rpc": f"{self.config.name}.rpc.*",
                "events": f"{self.config.name}.events.*",
            },
        }

        # Add feature-specific info
        if hasattr(self, "_websocket_handlers"):
            info["websocket_handlers"] = list(self._websocket_handlers.keys())

        if hasattr(self, "_broadcast_handlers"):
            info["broadcast_patterns"] = list(self._broadcast_handlers.keys())

        return info


# Pre-configured service classes using mixins
class NATSService(CliffracerService):
    """Basic NATS service - just core functionality"""

    pass


class ValidatedNATSService(ValidationMixin, CliffracerService):
    """NATS service with schema validation"""

    pass


class HTTPNATSService(HTTPMixin, CliffracerService):
    """NATS service with HTTP/REST API"""

    pass


class WebSocketNATSService(WebSocketMixin, HTTPMixin, CliffracerService):
    """NATS service with WebSocket support (requires HTTP)"""

    def __init__(self, config: ServiceConfig, **kwargs):
        # Initialize all parent classes first
        super().__init__(config, **kwargs)
        # Now discover handlers after all mixins are initialized
        self._discover_handlers()


class BroadcastNATSService(BroadcastMixin, CliffracerService):
    """NATS service with broadcast messaging"""

    pass


class FullFeaturedService(
    PerformanceMixin, BroadcastMixin, WebSocketMixin, HTTPMixin, ValidationMixin, CliffracerService
):
    """Service with all features enabled"""

    pass


class HighPerformanceService(PerformanceMixin, CliffracerService):
    """Service optimized for high performance"""

    pass


# Legacy aliases for backward compatibility
BaseNATSService = CliffracerService
ExtendedNATSService = ValidatedNATSService
