"""
Extended NATS microservice framework with HTTP, WebSocket, and schema validation
"""

import asyncio
import inspect
import json
import logging
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from nats_service import NATSService, ServiceConfig, NATSServiceMeta

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class Message(BaseModel):
    """Base message class for all service communications"""

    timestamp: datetime = None
    correlation_id: str | None = None

    def __init__(self, **data):
        if "timestamp" not in data:
            data["timestamp"] = datetime.now(UTC)
        super().__init__(**data)


class RPCRequest(Message):
    """Base class for RPC requests"""

    pass


class RPCResponse(Message):
    """Base class for RPC responses"""

    success: bool = True
    error: str | None = None
    traceback: str | None = None


class BroadcastMessage(Message):
    """Base class for broadcast messages"""

    source_service: str


def broadcast(message_class: type[Message], subject: str | None = None):
    """
    Decorator for methods that broadcast messages to all listeners

    @broadcast(UserCreatedMessage, subject="users.created")
    async def announce_user_created(self, user_id: str, email: str):
        return UserCreatedMessage(user_id=user_id, email=email)
    """

    def decorator(func: Callable) -> Callable:
        func._is_broadcast = True
        func._broadcast_message_class = message_class
        func._broadcast_subject = subject or f"broadcast.{message_class.__name__.lower()}"
        return func

    return decorator


def listener(message_class: type[Message], subject: str | None = None):
    """
    Decorator for methods that listen for broadcast messages

    @listener(UserCreatedMessage)
    async def on_user_created(self, message: UserCreatedMessage):
        print(f"New user created: {message.user_id}")
    """

    def decorator(func: Callable) -> Callable:
        # Determine subject from message class if not provided
        listen_subject = subject or f"broadcast.{message_class.__name__.lower()}"

        # This makes it work like event_handler but with schema validation
        func._is_event_handler = True
        func._event_pattern = listen_subject
        func._message_class = message_class
        func._is_listener = True
        return func

    return decorator


def validated_rpc(request_class: type[RPCRequest], response_class: type[RPCResponse]):
    """
    Decorator for RPC methods with request/response validation

    @validated_rpc(CreateOrderRequest, CreateOrderResponse)
    async def create_order(self, request: CreateOrderRequest) -> CreateOrderResponse:
        # request is already validated
        order = await self.process_order(request)
        return CreateOrderResponse(order=order)
    """

    def decorator(func: Callable) -> Callable:
        func._is_rpc = True
        func._rpc_name = func.__name__
        func._request_class = request_class
        func._response_class = response_class
        func._is_validated_rpc = True
        return func

    return decorator


class SchemaValidationMixin:
    """Mixin to add schema validation to services"""

    # These attributes will be provided by the base Service class
    _rpc_handlers: dict[str, Callable]
    _event_handlers: dict[str, Callable]
    config: ServiceConfig

    # Methods expected from base class
    async def call_rpc(self, service: str, method: str, **kwargs) -> Any:
        """Expected to be implemented by base class"""
        ...

    async def publish_event(self, subject: str, **kwargs):
        """Expected to be implemented by base class"""
        ...

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

    async def _handle_rpc_request(self, msg):
        """Enhanced RPC handler with schema validation"""
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
            # Check if this is a validated RPC
            if hasattr(handler, "_is_validated_rpc"):
                # Parse and validate request
                data = json.loads(msg.data.decode()) if msg.data else {}
                request_class = handler._request_class
                response_class = handler._response_class

                try:
                    request = request_class(**data)
                except ValidationError as e:
                    error_response = RPCResponse(
                        success=False, error="Validation error", traceback=str(e)
                    )
                    await msg.respond(error_response.model_dump_json().encode())
                    return

                # Call handler with validated request
                if inspect.iscoroutinefunction(handler):
                    response = await handler(self, request)
                else:
                    response = handler(self, request)

                # Ensure response is of correct type
                if not isinstance(response, response_class):
                    response = response_class(
                        **response.dict() if hasattr(response, "dict") else response
                    )

                await msg.respond(response.model_dump_json().encode())
            else:
                # Fall back to original behavior for non-validated RPCs
                data = json.loads(msg.data.decode()) if msg.data else {}

                if inspect.iscoroutinefunction(handler):
                    result = await handler(self, **data)
                else:
                    result = handler(self, **data)

                response = {"result": result, "timestamp": datetime.now(UTC).isoformat()}
                await msg.respond(json.dumps(response).encode())

        except Exception as e:
            logger.error(f"Error handling RPC request {handler_name}: {e}")
            error_response = RPCResponse(
                success=False,
                error=str(e),
                traceback=traceback.format_exc() if logger.level <= logging.DEBUG else None,
            )
            await msg.respond(error_response.model_dump_json().encode())

    async def _handle_event(self, msg):
        """Enhanced event handler with listener schema validation"""
        subject = msg.subject

        for pattern, handler in self._event_handlers.items():
            if self._subject_matches(pattern, subject):
                try:
                    data = json.loads(msg.data.decode()) if msg.data else {}

                    # Check if this is a listener with schema validation
                    if hasattr(handler, "_is_listener") and hasattr(handler, "_message_class"):
                        message_class = handler._message_class
                        try:
                            message = message_class(**data)
                            if inspect.iscoroutinefunction(handler):
                                await handler(self, message)
                            else:
                                handler(self, message)
                        except ValidationError as e:
                            logger.error(f"Validation error for listener {handler.__name__}: {e}")
                    else:
                        # Fall back to original behavior
                        if inspect.iscoroutinefunction(handler):
                            await handler(self, subject=subject, **data)
                        else:
                            handler(self, subject=subject, **data)

                except Exception as e:
                    logger.error(f"Error handling event {subject}: {e}")
                    logger.error(traceback.format_exc())

    async def broadcast_message(self, message: Message, subject: str | None = None):
        """Broadcast a message to all listeners"""
        if not isinstance(message, BroadcastMessage):
            # Add source service info if not already a broadcast message
            data = message.model_dump()
            data["source_service"] = self.config.name
            message = BroadcastMessage(**data)

        broadcast_subject = subject or f"broadcast.{message.__class__.__name__.lower()}"
        await self.publish_event(broadcast_subject, **message.model_dump())

    async def call_rpc_validated(
        self, service: str, method: str, request: RPCRequest, response_class: type[T]
    ) -> T:
        """Call an RPC method with validated request/response"""
        # This method expects the base class to have call_rpc
        response_data = await self.call_rpc(service, method, **request.model_dump())
        return response_class(**response_data)

    async def call_async_validated(self, service: str, method: str, request: RPCRequest):
        """Call an async RPC method with validated request (fire-and-forget)"""
        await self.call_async(service, method, **request.model_dump())


# Enhanced metaclass to handle all decorators
class ValidatedNATSServiceMeta(NATSServiceMeta):
    """Metaclass to collect all decorated methods"""

    def __new__(mcs, name, bases, namespace):
        cls = super().__new__(mcs, name, bases, namespace)

        # Collect all decorated methods
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)

            # RPC methods (including validated ones)
            if hasattr(attr, "_is_rpc"):
                if not hasattr(cls, "_rpc_methods"):
                    cls._rpc_methods = {}
                cls._rpc_methods[attr._rpc_name] = attr

            # Event handlers (including listeners)
            elif hasattr(attr, "_is_event_handler"):
                if not hasattr(cls, "_event_methods"):
                    cls._event_methods = {}
                cls._event_methods[attr._event_pattern] = attr

            # Broadcast methods
            elif hasattr(attr, "_is_broadcast"):
                if not hasattr(cls, "_broadcast_methods"):
                    cls._broadcast_methods = {}
                cls._broadcast_methods[attr_name] = attr

        return cls


class ValidatedNATSService(NATSService, SchemaValidationMixin, metaclass=ValidatedNATSServiceMeta):
    """Extended service with schema validation and broadcast support"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self._broadcast_methods = {}

        # Register broadcast methods
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if hasattr(attr, "_is_broadcast"):
                self._broadcast_methods[attr_name] = attr


class HTTPNATSService(ValidatedNATSService):
    """Service with HTTP/REST API support via FastAPI"""

    def __init__(self, config: ServiceConfig, host: str = "0.0.0.0", port: int = 8000):
        super().__init__(config)
        self.host = host
        self.port = port
        self.app = FastAPI(title=f"{config.name} API")
        self._setup_routes()
        self._server = None

    def _setup_routes(self):
        """Setup default routes"""

        @self.app.get("/health")
        async def health():
            return {
                "status": "healthy",
                "service": self.config.name,
                "nats_connected": self.nc and not self.nc.is_closed if self.nc else False,
            }

        @self.app.get("/info")
        async def info():
            return {
                "service": self.config.name,
                "rpc_methods": list(self._rpc_handlers.keys()),
                "event_handlers": list(self._event_handlers.keys()),
                "broadcast_methods": list(self._broadcast_methods.keys()),
            }

    def route(self, *args, **kwargs):
        """Decorator to add FastAPI routes"""
        return self.app.route(*args, **kwargs)

    def get(self, *args, **kwargs):
        """Decorator to add GET routes"""
        return self.app.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        """Decorator to add POST routes"""
        return self.app.post(*args, **kwargs)

    def put(self, *args, **kwargs):
        """Decorator to add PUT routes"""
        return self.app.put(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Decorator to add DELETE routes"""
        return self.app.delete(*args, **kwargs)

    async def start(self):
        """Start both NATS and HTTP server"""
        await super().start()

        # Start FastAPI server
        config = uvicorn.Config(app=self.app, host=self.host, port=self.port, log_level="info")
        self._server = uvicorn.Server(config)

        # Run server in background
        asyncio.create_task(self._server.serve())
        logger.info(f"HTTP server started on {self.host}:{self.port}")

    async def stop(self):
        """Stop both NATS and HTTP server"""
        if self._server:
            self._server.should_exit = True
        await super().stop()


class WebSocketNATSService(HTTPNATSService):
    """Service with WebSocket support"""

    def __init__(self, config: ServiceConfig, host: str = "0.0.0.0", port: int = 8000):
        super().__init__(config, host, port)
        self._websocket_handlers = {}
        self._active_connections: list[WebSocket] = []

    def websocket_handler(self, path: str):
        """Decorator to add WebSocket handlers"""

        def decorator(func: Callable) -> Callable:
            @self.app.websocket(path)
            async def websocket_endpoint(websocket: WebSocket):
                await self._handle_websocket(websocket, func)

            return func

        return decorator

    async def _handle_websocket(self, websocket: WebSocket, handler: Callable):
        """Handle WebSocket connections"""
        await websocket.accept()
        self._active_connections.append(websocket)

        try:
            # Call the handler
            await handler(self, websocket)
        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            if websocket in self._active_connections:
                self._active_connections.remove(websocket)

    async def broadcast_to_websockets(self, message: dict):
        """Broadcast message to all connected WebSocket clients"""
        disconnected = []
        for connection in self._active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)

        # Clean up disconnected clients
        for conn in disconnected:
            self._active_connections.remove(conn)

    @listener(BroadcastMessage)
    async def relay_broadcasts_to_websockets(self, message: BroadcastMessage):
        """Automatically relay NATS broadcasts to WebSocket clients"""
        await self.broadcast_to_websockets(message.model_dump())

