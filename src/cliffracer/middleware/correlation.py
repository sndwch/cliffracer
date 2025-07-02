"""
Correlation ID Middleware for HTTP and WebSocket connections

This module provides middleware for automatic correlation ID propagation
through HTTP requests and WebSocket connections.
"""

from collections.abc import Callable

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from ..core.correlation import CorrelationContext


class CorrelationMiddleware(BaseHTTPMiddleware):
    """
    FastAPI/Starlette middleware for correlation ID handling.

    Features:
    - Extracts correlation ID from incoming requests
    - Generates new ID if not present
    - Injects ID into response headers
    - Sets context for request lifecycle
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Extract or create correlation ID
        correlation_id = CorrelationContext.extract_from_headers(dict(request.headers))

        if not correlation_id:
            correlation_id = CorrelationContext.get_or_create_id()
            logger.info(f"Generated new correlation ID: {correlation_id}")
        else:
            logger.info(f"Extracted correlation ID: {correlation_id}")

        # Set in context
        CorrelationContext.set(correlation_id)

        # Add to request state for easy access
        request.state.correlation_id = correlation_id

        try:
            # Process request
            response = await call_next(request)

            # Add correlation ID to response headers
            response.headers["X-Correlation-ID"] = correlation_id

            return response

        finally:
            # Clear context after request
            CorrelationContext.clear()


class WebSocketCorrelationMiddleware:
    """
    WebSocket middleware for correlation ID handling.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "websocket":
            # Extract correlation ID from query params or headers
            headers = dict(scope.get("headers", []))
            query_string = scope.get("query_string", b"").decode()

            correlation_id = None

            # Check headers first
            for key, value in headers.items():
                if key.lower() in ["x-correlation-id", "correlation-id"]:
                    correlation_id = value.decode() if isinstance(value, bytes) else value
                    break

            # Check query params if not in headers
            if not correlation_id and "correlation_id=" in query_string:
                parts = query_string.split("correlation_id=")
                if len(parts) > 1:
                    correlation_id = parts[1].split("&")[0]

            # Generate if not found
            if not correlation_id:
                correlation_id = CorrelationContext.get_or_create_id()

            # Set in context
            CorrelationContext.set(correlation_id)

            # Add to scope for handlers
            scope["correlation_id"] = correlation_id

            logger.info(f"WebSocket connection with correlation ID: {correlation_id}")

            try:
                await self.app(scope, receive, send)
            finally:
                CorrelationContext.clear()
        else:
            # Pass through non-websocket requests
            await self.app(scope, receive, send)


def correlation_id_dependency(request: Request) -> str:
    """
    FastAPI dependency to inject correlation ID into route handlers.

    Usage:
        @app.get("/api/endpoint")
        async def my_endpoint(correlation_id: str = Depends(correlation_id_dependency)):
            # Use correlation_id here
            pass
    """
    return getattr(request.state, "correlation_id", None) or CorrelationContext.get_or_create_id()
