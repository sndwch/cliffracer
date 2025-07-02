"""
Feature mixins for Cliffracer services

This module provides composable mixins that can be combined to create
services with specific capabilities without inheritance complexity.
"""

import asyncio
import inspect
import json
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel, ValidationError

from .correlation import CorrelationContext, with_correlation_id

T = TypeVar("T", bound=BaseModel)


class ValidationMixin:
    """
    Mixin for schema validation using Pydantic models
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._validated_rpc_handlers: dict[str, tuple[Callable, type]] = {}

    def register_validated_rpc(self, method_name: str, handler: Callable, schema: type):
        """Register a validated RPC handler with its schema"""
        self._validated_rpc_handlers[method_name] = (handler, schema)
        # Also register as regular RPC handler
        self._rpc_handlers[method_name] = handler

    async def _handle_rpc_request(self, msg):
        """Handle RPC requests with schema validation"""
        subject = msg.subject
        handler_name = subject.split(".")[-1]

        if handler_name not in self._validated_rpc_handlers:
            # Fall back to standard RPC handling
            return await super()._handle_rpc_request_base(msg)

        handler, schema = self._validated_rpc_handlers[handler_name]

        try:
            # Parse and validate request data
            raw_data = json.loads(msg.data.decode()) if msg.data else {}

            # Validate using schema
            validated_data = schema(**raw_data)

            # Call handler with validated data
            if inspect.iscoroutinefunction(handler):
                result = await handler(validated_data)
            else:
                result = handler(validated_data)

            # Convert result to dict if it's a Pydantic model
            if isinstance(result, BaseModel):
                result_data = result.model_dump(mode='json')
            else:
                result_data = result
            
            # Send response
            response = {"result": result_data, "timestamp": datetime.now(UTC).isoformat()}
            await msg.respond(json.dumps(response).encode())

        except ValidationError as e:
            error_response = {
                "error": f"Validation error: {e}",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            await msg.respond(json.dumps(error_response).encode())

        except Exception as e:
            logger.error(f"Error handling validated RPC request {handler_name}: {e}")
            error_response = {
                "error": str(e),
                "traceback": traceback.format_exc(),
                "timestamp": datetime.now(UTC).isoformat(),
            }
            await msg.respond(json.dumps(error_response).encode())


class HTTPMixin:
    """
    Mixin for HTTP/REST API functionality using FastAPI
    """

    def __init__(self, *args, host: str = "0.0.0.0", port: int = 8000, **kwargs):
        super().__init__(*args, **kwargs)
        self.host = host
        self.port = port
        self.app = FastAPI(title=f"{self.config.name} API")
        self._http_server = None
        self._http_running = False

        # Add correlation middleware
        from ..middleware.correlation import CorrelationMiddleware
        self.app.add_middleware(CorrelationMiddleware)

        # Add health check endpoint
        @self.app.get("/health")
        @with_correlation_id
        async def health_check():
            return await self.health_check()

        # Add service info endpoint
        @self.app.get("/info")
        @with_correlation_id
        async def service_info():
            return self.get_service_info()

    async def start_http(self):
        """Start the HTTP server"""
        if self._http_running:
            return

        logger.info(f"Starting HTTP server on {self.host}:{self.port}")

        config = uvicorn.Config(
            app=self.app,
            host=self.host,
            port=self.port,
            log_level="info"
        )
        self._http_server = uvicorn.Server(config)

        # Start server in background task
        self._http_task = asyncio.create_task(self._http_server.serve())
        self._http_running = True

        logger.info(f"HTTP server started on http://{self.host}:{self.port}")

    async def stop_http(self):
        """Stop the HTTP server"""
        if not self._http_running:
            return

        logger.info("Stopping HTTP server")

        if self._http_server:
            self._http_server.should_exit = True
            await self._http_task

        self._http_running = False
        logger.info("HTTP server stopped")

    def get(self, path: str, **kwargs):
        """Decorator for GET endpoints"""
        return self.app.get(path, **kwargs)

    def post(self, path: str, **kwargs):
        """Decorator for POST endpoints"""
        return self.app.post(path, **kwargs)

    def put(self, path: str, **kwargs):
        """Decorator for PUT endpoints"""
        return self.app.put(path, **kwargs)

    def delete(self, path: str, **kwargs):
        """Decorator for DELETE endpoints"""
        return self.app.delete(path, **kwargs)


class WebSocketMixin:
    """
    Mixin for WebSocket functionality
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._active_connections: set[WebSocket] = set()
        self._websocket_handlers: dict[str, Callable] = {}

        # Add WebSocket endpoint if HTTP mixin is present
        if hasattr(self, 'app'):
            @self.app.websocket("/ws")
            async def websocket_endpoint(websocket: WebSocket):
                await self._handle_websocket_connection(websocket)

    def register_websocket_handler(self, path: str, handler: Callable):
        """Register a WebSocket handler for a specific path"""
        self._websocket_handlers[path] = handler

        # Add dynamic endpoint if HTTP mixin is present
        if hasattr(self, 'app'):
            @self.app.websocket(path)
            async def dynamic_endpoint(websocket: WebSocket):
                await self._handle_websocket(websocket, handler)

    async def _handle_websocket_connection(self, websocket: WebSocket):
        """Handle generic WebSocket connections"""
        await websocket.accept()
        self._active_connections.add(websocket)

        # Extract correlation ID from WebSocket
        correlation_id = None
        if hasattr(websocket, 'scope'):
            correlation_id = websocket.scope.get('correlation_id')
        if not correlation_id:
            correlation_id = CorrelationContext.get_or_create_id()

        CorrelationContext.set(correlation_id)
        logger.info(f"WebSocket connection established with correlation_id: {correlation_id}")

        try:
            while True:
                # Keep connection alive and handle messages
                message = await websocket.receive_text()
                data = json.loads(message)

                # Extract correlation ID from message if present
                msg_correlation_id = data.get('correlation_id', correlation_id)
                CorrelationContext.set(msg_correlation_id)

                # Echo back with correlation ID
                await websocket.send_text(json.dumps({
                    "type": "echo",
                    "data": data,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "correlation_id": msg_correlation_id
                }))

        except WebSocketDisconnect:
            logger.info(f"WebSocket client disconnected (correlation_id: {correlation_id})")
        except Exception as e:
            logger.error(f"WebSocket error (correlation_id: {correlation_id}): {e}")
        finally:
            self._active_connections.discard(websocket)

    async def _handle_websocket(self, websocket: WebSocket, handler: Callable):
        """Handle WebSocket with specific handler"""
        await websocket.accept()
        self._active_connections.add(websocket)

        try:
            if inspect.iscoroutinefunction(handler):
                await handler(self, websocket)
            else:
                handler(self, websocket)
        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        except Exception as e:
            logger.error(f"WebSocket handler error: {e}")
        finally:
            self._active_connections.discard(websocket)

    async def broadcast_to_websockets(self, message: dict):
        """Broadcast message to all connected WebSocket clients"""
        if not self._active_connections:
            return

        message_str = json.dumps(message)
        disconnected = set()

        for websocket in self._active_connections:
            try:
                await websocket.send_text(message_str)
            except Exception:
                disconnected.add(websocket)

        # Remove disconnected clients
        self._active_connections -= disconnected

        logger.debug(f"Broadcasted to {len(self._active_connections)} WebSocket clients")

    def get_websocket_stats(self) -> dict[str, Any]:
        """Get WebSocket connection statistics"""
        return {
            "active_connections": len(self._active_connections),
            "registered_handlers": len(self._websocket_handlers)
        }


class BroadcastMixin:
    """
    Mixin for event broadcasting functionality
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._broadcast_handlers: dict[str, Callable] = {}

    def register_broadcast_handler(self, pattern: str, handler: Callable):
        """Register a broadcast handler for message patterns"""
        self._broadcast_handlers[pattern] = handler
        # Also register as event handler
        self._event_handlers[pattern] = handler

    async def broadcast_message(self, subject: str, **kwargs):
        """Broadcast a message to all interested parties"""
        message = {
            "data": kwargs,
            "timestamp": datetime.now(UTC).isoformat(),
            "source_service": self.config.name
        }

        # Publish to NATS
        await self.publish_event(subject, **message)

        # Broadcast to WebSocket clients if available
        if hasattr(self, 'broadcast_to_websockets'):
            await self.broadcast_to_websockets({
                "type": "broadcast",
                "subject": subject,
                "data": kwargs,
                "timestamp": message["timestamp"]
            })

        logger.debug(f"Broadcasted message: {subject}")


class PerformanceMixin:
    """
    Mixin for performance optimization features
    """

    def __init__(self, *args,
                 enable_connection_pooling: bool = False,
                 enable_batch_processing: bool = False,
                 enable_metrics: bool = False,
                 **kwargs):
        super().__init__(*args, **kwargs)

        # Initialize performance features if requested
        if enable_connection_pooling:
            from ..performance.connection_pool import OptimizedNATSConnection
            self._connection_pool = OptimizedNATSConnection(
                nats_url=self.config.nats_url,
                max_connections=kwargs.get('connection_pool_size', 5)
            )
        else:
            self._connection_pool = None

        if enable_batch_processing:
            from ..performance.batch_processor import BatchProcessor
            self._batch_processor = BatchProcessor(
                batch_size=kwargs.get('batch_size', 50),
                batch_timeout_ms=kwargs.get('batch_timeout_ms', 25)
            )
        else:
            self._batch_processor = None

        if enable_metrics:
            from ..performance.metrics import PerformanceMetrics
            self._metrics = PerformanceMetrics()
        else:
            self._metrics = None

    async def start_performance_features(self):
        """Start performance optimization features"""
        if self._connection_pool:
            await self._connection_pool.connect()
            logger.info("Optimized connection pool initialized")

        if self._metrics:
            self._metrics.record_connection_event("service_started")

    async def stop_performance_features(self):
        """Stop performance optimization features"""
        if self._batch_processor:
            await self._batch_processor.flush_all()

        if self._connection_pool:
            await self._connection_pool.close()

    def get_performance_metrics(self) -> dict[str, Any]:
        """Get performance metrics if available"""
        if not self._metrics:
            return {"error": "Metrics not enabled"}

        metrics = self._metrics.get_performance_summary()

        if self._connection_pool:
            metrics["connection_pool"] = self._connection_pool.get_stats()

        if self._batch_processor:
            metrics["batch_processor"] = self._batch_processor.get_stats()

        return metrics
