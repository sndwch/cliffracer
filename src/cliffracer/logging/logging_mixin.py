"""
Logging mixin for adding comprehensive logging to any service
"""

import asyncio
import functools
import time
from typing import Any

from loguru import logger


class LoggingMixin:
    """
    Mixin that adds comprehensive logging to any service class.

    This replaces the need for separate LoggedExtendedService,
    LoggedHTTPService, and LoggedWebSocketService classes.
    """

    def __init__(self, *args, **kwargs):
        """Initialize with contextual logger"""
        super().__init__(*args, **kwargs)

        # Create contextual logger for this service
        self.logger = logger.bind(
            service=self.config.name,
            service_type=self.__class__.__name__,
        )

        # Wrap methods with logging
        self._wrap_methods_with_logging()

    def _wrap_methods_with_logging(self):
        """Wrap key methods with logging decorators"""
        # List of methods to wrap with logging
        methods_to_wrap = [
            'start', 'stop', 'connect', 'disconnect',
            '_handle_rpc_request', '_handle_event', '_handle_async_request',
            'call_rpc', 'call_async', 'publish_event',
        ]

        for method_name in methods_to_wrap:
            if hasattr(self, method_name):
                original_method = getattr(self, method_name)
                if not hasattr(original_method, '_logged'):
                    wrapped_method = self._log_method_execution(original_method, method_name)
                    wrapped_method._logged = True
                    setattr(self, method_name, wrapped_method)

    def _log_method_execution(self, method, method_name):
        """Decorator to log method execution"""
        @functools.wraps(method)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            context = {
                'method': method_name,
                'args_count': len(args) - 1,  # Exclude self
                'kwargs_keys': list(kwargs.keys()),
            }

            self.logger.debug(f"Starting {method_name}", **context)

            try:
                result = await method(*args, **kwargs)

                execution_time = time.time() - start_time
                self.logger.info(
                    f"Completed {method_name}",
                    execution_time=execution_time,
                    **context
                )

                # Record metrics if available
                if hasattr(self, 'record_metric'):
                    await self.record_metric(
                        f"method.{method_name}.duration",
                        execution_time,
                        {"status": "success"}
                    )
                    await self.record_metric(
                        f"method.{method_name}.count",
                        1,
                        {"status": "success"}
                    )

                return result

            except Exception as e:
                execution_time = time.time() - start_time
                self.logger.error(
                    f"Error in {method_name}: {str(e)}",
                    execution_time=execution_time,
                    error_type=type(e).__name__,
                    **context
                )

                # Record error metrics if available
                if hasattr(self, 'record_metric'):
                    await self.record_metric(
                        f"method.{method_name}.duration",
                        execution_time,
                        {"status": "error"}
                    )
                    await self.record_metric(
                        f"method.{method_name}.count",
                        1,
                        {"status": "error", "error_type": type(e).__name__}
                    )

                raise

        @functools.wraps(method)
        def sync_wrapper(*args, **kwargs):
            start_time = time.time()
            context = {
                'method': method_name,
                'args_count': len(args) - 1,  # Exclude self
                'kwargs_keys': list(kwargs.keys()),
            }

            self.logger.debug(f"Starting {method_name}", **context)

            try:
                result = method(*args, **kwargs)

                execution_time = time.time() - start_time
                self.logger.info(
                    f"Completed {method_name}",
                    execution_time=execution_time,
                    **context
                )

                return result

            except Exception as e:
                execution_time = time.time() - start_time
                self.logger.error(
                    f"Error in {method_name}: {str(e)}",
                    execution_time=execution_time,
                    error_type=type(e).__name__,
                    **context
                )
                raise

        # Return appropriate wrapper based on method type
        if asyncio.iscoroutinefunction(method):
            return async_wrapper
        else:
            return sync_wrapper

    async def on_startup(self):
        """Log service startup"""
        self.logger.info(
            "Service starting up",
            version=self.config.version,
            description=self.config.description,
        )
        await super().on_startup()

    async def on_shutdown(self):
        """Log service shutdown"""
        self.logger.info("Service shutting down")
        await super().on_shutdown()


class HTTPLoggingMixin(LoggingMixin):
    """Extended logging for HTTP services"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Add HTTP request logging middleware if FastAPI app exists
        if hasattr(self, 'app'):
            self._add_http_logging_middleware()

    def _add_http_logging_middleware(self):
        """Add HTTP request/response logging middleware"""
        @self.app.middleware("http")
        async def log_requests(request, call_next):
            start_time = time.time()

            # Log request
            self.logger.info(
                "HTTP request received",
                method=request.method,
                path=request.url.path,
                client=request.client.host if request.client else None,
            )

            # Process request
            response = await call_next(request)

            # Log response
            process_time = time.time() - start_time
            self.logger.info(
                "HTTP response sent",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                process_time=process_time,
            )

            # Add process time header
            response.headers["X-Process-Time"] = str(process_time)

            return response


class WebSocketLoggingMixin(HTTPLoggingMixin):
    """Extended logging for WebSocket services"""

    async def broadcast_to_websockets(self, data: dict[str, Any]):
        """Log WebSocket broadcasts"""
        self.logger.debug(
            "Broadcasting to WebSocket clients",
            client_count=len(self._active_connections),
            data_type=data.get("type", "unknown"),
        )

        result = await super().broadcast_to_websockets(data)

        self.logger.info(
            "WebSocket broadcast completed",
            clients_notified=len(self._active_connections),
        )

        return result

    async def _handle_websocket(self, websocket, handler):
        """Log WebSocket connections"""
        client_info = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"

        self.logger.info(
            "WebSocket connection established",
            client=client_info,
            handler=handler.__name__,
        )

        try:
            await super()._handle_websocket(websocket, handler)
        finally:
            self.logger.info(
                "WebSocket connection closed",
                client=client_info,
            )
