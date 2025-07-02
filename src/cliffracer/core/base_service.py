"""
Cliffracer NATS-based microservice framework inspired by Nameko
Combines NATS messaging with auto-restarting runners for resilient services
"""

import asyncio
import inspect
import json
import logging
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import nats
from nats.errors import TimeoutError
from nats.js import JetStreamContext

from .service_config import ServiceConfig

logger = logging.getLogger(__name__)


class BaseNATSService:
    """Base class for NATS-based microservices"""

    def __init__(self, config: ServiceConfig):
        self.config = config
        self.nc: nats.NATS | None = None
        self.js: JetStreamContext | None = None
        self._subscriptions: set[asyncio.Task] = set()
        self._running = False
        self._rpc_handlers: dict[str, Callable] = {}
        self._event_handlers: dict[str, Callable] = {}
        self._backdoor_server: Any | None = None
        self._timers: list[Any] = []  # Will be Timer instances

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
        # Stop backdoor server
        await self._stop_backdoor()

        if self.nc and not self.nc.is_closed:
            await self.nc.drain()
            await self.nc.close()

    async def _error_callback(self, e):
        logger.error(f"NATS error: {e}")

    async def _disconnected_callback(self):
        logger.warning(f"Service '{self.config.name}' disconnected from NATS")

    async def _reconnected_callback(self):
        logger.info(f"Service '{self.config.name}' reconnected to NATS")

    async def _closed_callback(self):
        logger.info(f"Service '{self.config.name}' connection closed")

    async def _handle_rpc_request(self, msg):
        """Handle incoming RPC requests"""
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

            # Call handler
            if inspect.iscoroutinefunction(handler):
                result = await handler(self, **data)
            else:
                result = handler(self, **data)

            # Send response
            response = {"result": result, "timestamp": datetime.now(UTC).isoformat()}
            await msg.respond(json.dumps(response).encode())

        except Exception as e:
            logger.error(f"Error handling RPC request {handler_name}: {e}")
            error_response = {
                "error": str(e),
                "traceback": traceback.format_exc(),
                "timestamp": datetime.now(UTC).isoformat(),
            }
            await msg.respond(json.dumps(error_response).encode())

    async def _handle_async_request(self, msg):
        """Handle incoming async (fire-and-forget) requests"""
        subject = msg.subject
        handler_name = subject.split(".")[-1]

        if handler_name not in self._rpc_handlers:
            # For async calls, we silently ignore unknown methods
            logger.warning(f"Unknown async method: {handler_name}")
            return

        handler = self._rpc_handlers[handler_name]

        try:
            # Parse request data
            data = json.loads(msg.data.decode()) if msg.data else {}

            # Call handler (no response expected)
            if inspect.iscoroutinefunction(handler):
                await handler(self, **data)
            else:
                handler(self, **data)

        except Exception as e:
            # For async calls, we log errors but don't send responses
            logger.error(f"Error handling async request {handler_name}: {e}")
            logger.error(traceback.format_exc())

    async def _handle_event(self, msg):
        """Handle incoming events"""
        subject = msg.subject

        for pattern, handler in self._event_handlers.items():
            if self._subject_matches(pattern, subject):
                try:
                    data = json.loads(msg.data.decode()) if msg.data else {}

                    if inspect.iscoroutinefunction(handler):
                        await handler(self, subject=subject, **data)
                    else:
                        handler(self, subject=subject, **data)

                except Exception as e:
                    logger.error(f"Error handling event {subject}: {e}")
                    logger.error(traceback.format_exc())

    def _subject_matches(self, pattern: str, subject: str) -> bool:
        """Check if subject matches pattern (supports wildcards)"""
        pattern_parts = pattern.split(".")
        subject_parts = subject.split(".")

        if len(pattern_parts) != len(subject_parts) and ">" not in pattern:
            return False

        for _i, (p, s) in enumerate(zip(pattern_parts, subject_parts, strict=False)):
            if p == ">":
                return True
            elif p == "*":
                continue
            elif p != s:
                return False

        return True

    async def _discover_timers(self):
        """Discover and register timer-decorated methods"""
        for name in dir(self):
            if name.startswith("_"):
                continue

            method = getattr(self, name)
            if hasattr(method, "_cliffracer_timers"):
                for timer_instance in method._cliffracer_timers:
                    self._timers.append(timer_instance)
                    logger.debug(f"Discovered timer: {timer_instance.method_name}")

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

    async def start(self):
        """Start the service and subscribe to subjects"""
        await self.connect()
        self._running = True

        # Discover and register timers
        await self._discover_timers()

        # Subscribe to RPC subjects (synchronous - expects response)
        rpc_subject = f"{self.config.name}.rpc.*"
        sub = await self.nc.subscribe(rpc_subject, cb=self._handle_rpc_request)
        self._subscriptions.add(asyncio.create_task(self._subscription_handler(sub)))

        # Subscribe to async subjects (fire-and-forget - no response)
        async_subject = f"{self.config.name}.async.*"
        sub = await self.nc.subscribe(async_subject, cb=self._handle_async_request)
        self._subscriptions.add(asyncio.create_task(self._subscription_handler(sub)))

        # Subscribe to event subjects
        for pattern in self._event_handlers:
            sub = await self.nc.subscribe(pattern, cb=self._handle_event)
            self._subscriptions.add(asyncio.create_task(self._subscription_handler(sub)))

        # Start all timers
        await self._start_timers()

        timer_count = len(self._timers)
        logger.info(
            f"Service '{self.config.name}' started with {len(self._rpc_handlers)} RPC handlers, "
            f"{len(self._event_handlers)} event handlers, and {timer_count} timers"
        )

    async def _subscription_handler(self, sub):
        """Handle subscription lifecycle"""
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            await sub.unsubscribe()

    async def stop(self):
        """Stop the service"""
        self._running = False

        # Stop all timers
        await self._stop_timers()

        # Cancel all subscriptions
        for task in self._subscriptions:
            task.cancel()

        await asyncio.gather(*self._subscriptions, return_exceptions=True)
        self._subscriptions.clear()

        await self.disconnect()
        logger.info(f"Service '{self.config.name}' stopped")

    def get_service_info(self) -> dict[str, Any]:
        """Get service metadata for client generation"""
        return {
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

    async def call_rpc(self, service: str, method: str, **kwargs) -> Any:
        """
        Call an RPC method on another service (synchronous - waits for response)

        Args:
            service: Target service name
            method: RPC method name
            **kwargs: Method arguments

        Returns:
            The response from the remote service

        Raises:
            Exception: If RPC call fails or times out
        """
        subject = f"{service}.rpc.{method}"
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
            raise Exception(f"RPC timeout calling {service}.{method}") from e

    async def call_async(self, service: str, method: str, **kwargs):
        """
        Call an RPC method asynchronously (fire-and-forget - no response expected)

        This is useful for triggering actions on other services without waiting
        for completion or caring about the result.

        Args:
            service: Target service name
            method: RPC method name
            **kwargs: Method arguments

        Note:
            No response is expected and no errors are propagated back.
            Use this for non-critical operations or when you don't need to know
            if the operation succeeded.
        """
        subject = f"{service}.async.{method}"
        request_data = json.dumps(kwargs).encode()
        await self.nc.publish(subject, request_data)

    async def call_rpc_no_wait(self, service: str, method: str, **kwargs):
        """
        Call an RPC method without waiting for response (fire-and-forget)

        Similar to call_async but uses the RPC subject pattern.
        The target service will process the request but no response is expected.

        Args:
            service: Target service name
            method: RPC method name
            **kwargs: Method arguments
        """
        subject = f"{service}.rpc.{method}"
        request_data = json.dumps(kwargs).encode()
        await self.nc.publish(subject, request_data)

    async def publish_event(self, subject: str, **kwargs):
        """Publish an event"""
        event_data = json.dumps(kwargs).encode()
        await self.nc.publish(subject, event_data)


def rpc(func: Callable) -> Callable:
    """Decorator to mark a method as an RPC handler (synchronous - returns response)"""
    func._is_rpc = True
    func._rpc_name = func.__name__
    return func


def async_rpc(func: Callable) -> Callable:
    """
    Decorator to mark a method as an async RPC handler (fire-and-forget)

    These methods can be called via call_async() and don't return responses.
    Useful for triggering background operations without waiting for completion.
    """
    func._is_rpc = True
    func._rpc_name = func.__name__
    func._is_async_rpc = True
    return func


def event_handler(subject_pattern: str) -> Callable:
    """Decorator to mark a method as an event handler"""

    def decorator(func: Callable) -> Callable:
        func._is_event_handler = True
        func._event_pattern = subject_pattern
        return func

    return decorator


# Add backdoor methods to BaseNATSService
async def _start_backdoor(self):
    """Start backdoor debugging server if enabled."""
    try:
        # Import here to avoid circular imports
        from ..debug.backdoor import BackdoorServer, is_backdoor_enabled

        if not is_backdoor_enabled(self.config):
            logger.debug("Backdoor server disabled")
            return

        if not self.config.backdoor_enabled or self.config.disable_backdoor:
            logger.debug("Backdoor server disabled in config")
            return

        self._backdoor_server = BackdoorServer(
            service_instance=self, port=self.config.backdoor_port, enabled=True
        )

        port = self._backdoor_server.start()
        if port:
            logger.info(f"🔧 Backdoor server available on localhost:{port}")

    except Exception as e:
        logger.warning(f"Failed to start backdoor server: {e}")
        self._backdoor_server = None


async def _stop_backdoor(self):
    """Stop backdoor debugging server."""
    if self._backdoor_server:
        try:
            self._backdoor_server.stop()
            self._backdoor_server = None
            logger.debug("Backdoor server stopped")
        except Exception as e:
            logger.warning(f"Error stopping backdoor server: {e}")


# Attach methods to BaseNATSService
BaseNATSService._start_backdoor = _start_backdoor
BaseNATSService._stop_backdoor = _stop_backdoor


class NATSServiceMeta(type):
    """Metaclass to collect decorated methods"""

    def __new__(mcs, name, bases, namespace):
        cls = super().__new__(mcs, name, bases, namespace)

        # Collect RPC handlers and event handlers
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)

            if hasattr(attr, "_is_rpc"):
                if not hasattr(cls, "_rpc_methods"):
                    cls._rpc_methods = {}
                cls._rpc_methods[attr._rpc_name] = attr

            elif hasattr(attr, "_is_event_handler"):
                if not hasattr(cls, "_event_methods"):
                    cls._event_methods = {}
                cls._event_methods[attr._event_pattern] = attr

        return cls


class NATSService(BaseNATSService, metaclass=NATSServiceMeta):
    """Enhanced service class that auto-registers decorated methods"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)

        # Register built-in introspection RPC method
        self._rpc_handlers["get_service_info"] = self._handle_get_service_info

        # Register RPC handlers
        if hasattr(self.__class__, "_rpc_methods"):
            for name, method in self.__class__._rpc_methods.items():
                self._rpc_handlers[name] = method

        # Register event handlers
        if hasattr(self.__class__, "_event_methods"):
            for pattern, method in self.__class__._event_methods.items():
                self._event_handlers[pattern] = method

    async def _handle_get_service_info(self, **kwargs) -> dict[str, Any]:
        """RPC handler for service introspection"""
        return self.get_service_info()
