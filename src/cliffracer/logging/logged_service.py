"""
Enhanced NATS services with structured logging using loguru
"""

import os

from .config import LoggingConfig, get_service_logger
from ..core.extended_service import (
    ValidatedNATSService,
    HTTPNATSService,
    ServiceConfig,
    WebSocketNATSService,
)


class LoggedExtendedService(ValidatedNATSService):
    """Extended service with structured logging"""

    def __init__(self, config: ServiceConfig):
        # Configure logging first
        LoggingConfig.configure(
            service_name=config.name,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            structured=os.getenv("LOG_FORMAT", "json").lower() == "json",
            log_dir=os.getenv("LOG_DIR", "./logs"),
        )

        # Get contextual logger
        self.logger = get_service_logger(config.name, service_type="extended")

        super().__init__(config)

        self.logger.info(
            "Service initialized",
            service_name=config.name,
            nats_url=config.nats_url,
            auto_restart=config.auto_restart,
        )

    async def connect(self):
        """Connect to NATS with logging"""
        self.logger.info("Connecting to NATS server", nats_url=self.config.nats_url)

        try:
            await super().connect()
            self.logger.info(
                "Successfully connected to NATS", connection_id=id(self.nc) if self.nc else None
            )
        except Exception as e:
            self.logger.error(
                "Failed to connect to NATS", error=str(e), error_type=type(e).__name__
            )
            raise

    async def disconnect(self):
        """Disconnect from NATS with logging"""
        self.logger.info("Disconnecting from NATS server")

        try:
            await super().disconnect()
            self.logger.info("Successfully disconnected from NATS")
        except Exception as e:
            self.logger.error("Error during NATS disconnection", error=str(e))
            raise

    async def start(self):
        """Start service with logging"""
        self.logger.info(
            "Starting service",
            rpc_handlers=len(self._rpc_handlers),
            event_handlers=len(self._event_handlers),
        )

        try:
            await super().start()
            self.logger.info("Service started successfully")
        except Exception as e:
            self.logger.error("Failed to start service", error=str(e))
            raise

    async def stop(self):
        """Stop service with logging"""
        self.logger.info("Stopping service")

        try:
            await super().stop()
            self.logger.info("Service stopped successfully")
        except Exception as e:
            self.logger.error("Error during service stop", error=str(e))
            raise

    async def _error_callback(self, e):
        """Enhanced error callback with logging"""
        self.logger.error("NATS connection error", error=str(e), error_type=type(e).__name__)
        await super()._error_callback(e)

    async def _disconnected_callback(self):
        """Enhanced disconnection callback with logging"""
        self.logger.warning("NATS connection lost")
        await super()._disconnected_callback()

    async def _reconnected_callback(self):
        """Enhanced reconnection callback with logging"""
        self.logger.info("NATS connection restored")
        await super()._reconnected_callback()

    async def call_rpc(self, service: str, method: str, **kwargs):
        """RPC call with logging"""
        call_logger = self.logger.with_context(
            target_service=service,
            rpc_method=method,
            call_id=id(kwargs),  # Simple call tracking
        )

        call_logger.debug("Making RPC call", arguments=list(kwargs.keys()))

        try:
            result = await super().call_rpc(service, method, **kwargs)
            call_logger.debug("RPC call successful", result_type=type(result).__name__)
            return result

        except Exception as e:
            call_logger.error("RPC call failed", error=str(e), error_type=type(e).__name__)
            raise

    async def publish_event(self, subject: str, **kwargs):
        """Event publishing with logging"""
        event_logger = self.logger.with_context(
            event_subject=subject, event_data_keys=list(kwargs.keys())
        )

        event_logger.debug("Publishing event")

        try:
            await super().publish_event(subject, **kwargs)
            event_logger.debug("Event published successfully")

        except Exception as e:
            event_logger.error("Failed to publish event", error=str(e))
            raise


class LoggedHTTPService(HTTPNATSService):
    """HTTP service with structured logging"""

    def __init__(self, config: ServiceConfig, host: str = "0.0.0.0", port: int = 8000):
        # Configure logging
        LoggingConfig.configure(
            service_name=config.name,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            structured=os.getenv("LOG_FORMAT", "json").lower() == "json",
            log_dir=os.getenv("LOG_DIR", "./logs"),
        )

        self.logger = get_service_logger(config.name, service_type="http", host=host, port=port)

        super().__init__(config, host, port)

        # Add request logging middleware
        self._setup_request_logging()

        self.logger.info("HTTP service initialized", host=host, port=port)

    def _setup_request_logging(self):
        """Setup FastAPI request logging middleware"""
        import time

        from fastapi import Request
        from starlette.middleware.base import BaseHTTPMiddleware

        class RequestLoggingMiddleware(BaseHTTPMiddleware):
            def __init__(self, app, logger):
                super().__init__(app)
                self.logger = logger

            async def dispatch(self, request: Request, call_next):
                start_time = time.time()

                # Log request
                request_logger = self.logger.with_context(
                    method=request.method,
                    url=str(request.url),
                    client_ip=request.client.host if request.client else "unknown",
                    user_agent=request.headers.get("user-agent", "unknown"),
                )

                request_logger.info("HTTP request received")

                try:
                    response = await call_next(request)
                    process_time = time.time() - start_time

                    request_logger.info(
                        "HTTP request completed",
                        status_code=response.status_code,
                        process_time_ms=round(process_time * 1000, 2),
                    )

                    return response

                except Exception as e:
                    process_time = time.time() - start_time
                    request_logger.error(
                        "HTTP request failed",
                        error=str(e),
                        process_time_ms=round(process_time * 1000, 2),
                    )
                    raise

        self.app.add_middleware(RequestLoggingMiddleware, logger=self.logger)

    async def start(self):
        """Start HTTP service with logging"""
        self.logger.info("Starting HTTP server")
        await super().start()
        self.logger.info("HTTP server started successfully")


class LoggedWebSocketService(WebSocketNATSService):
    """WebSocket service with structured logging"""

    def __init__(self, config: ServiceConfig, host: str = "0.0.0.0", port: int = 8000):
        # Configure logging
        LoggingConfig.configure(
            service_name=config.name,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            structured=os.getenv("LOG_FORMAT", "json").lower() == "json",
            log_dir=os.getenv("LOG_DIR", "./logs"),
        )

        self.logger = get_service_logger(
            config.name, service_type="websocket", host=host, port=port
        )

        super().__init__(config, host, port)

        self.logger.info("WebSocket service initialized")

    async def _handle_websocket(self, websocket, handler):
        """Handle WebSocket with logging"""
        client_ip = websocket.client.host if websocket.client else "unknown"
        ws_logger = self.logger.with_context(client_ip=client_ip, connection_id=id(websocket))

        ws_logger.info("WebSocket connection established")

        try:
            await super()._handle_websocket(websocket, handler)

        except Exception as e:
            ws_logger.error("WebSocket connection error", error=str(e))
            raise
        finally:
            ws_logger.info("WebSocket connection closed")

    async def broadcast_to_websockets(self, message: dict):
        """Broadcast with logging"""
        self.logger.debug(
            "Broadcasting to WebSocket clients",
            client_count=len(self._active_connections),
            message_type=message.get("type", "unknown"),
        )

        await super().broadcast_to_websockets(message)


# Convenience functions to create logged services
def create_logged_service(config: ServiceConfig) -> LoggedExtendedService:
    """Create a logged extended service"""
    return LoggedExtendedService(config)


def create_logged_http_service(
    config: ServiceConfig, host: str = "0.0.0.0", port: int = 8000
) -> LoggedHTTPService:
    """Create a logged HTTP service"""
    return LoggedHTTPService(config, host, port)


def create_logged_websocket_service(
    config: ServiceConfig, host: str = "0.0.0.0", port: int = 8000
) -> LoggedWebSocketService:
    """Create a logged WebSocket service"""
    return LoggedWebSocketService(config, host, port)
