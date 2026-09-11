"""HTTP correlation middleware.

Extracts incoming correlation headers or generates new identifiers, binds them
to ambient context for the request lifecycle, and echoes them on responses.
"""

from collections.abc import Callable
from typing import Any, cast

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from cliffracer.core.correlation import CorrelationContext


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Starlette middleware establishing correlation context per HTTP request."""

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
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
            response = cast(Response, await call_next(request))

            # Add correlation ID to response headers
            response.headers["X-Correlation-ID"] = correlation_id

            return response

        finally:
            # Clear context after request
            CorrelationContext.clear()


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
