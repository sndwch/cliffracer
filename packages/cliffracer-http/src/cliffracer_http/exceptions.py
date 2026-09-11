"""HTTP and WebSocket exception classes inheriting from core service exceptions."""

from typing import Any

from cliffracer.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConnectionError,
    HandlerError,
    ServiceError,
)


class HTTPError(ServiceError):
    """Base HTTP error"""

    def __init__(self, message: str, status_code: int = 500, details: dict[str, Any] | None = None):
        super().__init__(message, details)
        self.status_code = status_code


class HTTPNotFoundError(HTTPError):
    """HTTP 404 Not Found"""

    def __init__(self, message: str = "Not Found", details: dict[str, Any] | None = None):
        super().__init__(message, 404, details)


class HTTPBadRequestError(HTTPError):
    """HTTP 400 Bad Request"""

    def __init__(self, message: str = "Bad Request", details: dict[str, Any] | None = None):
        super().__init__(message, 400, details)


class HTTPUnauthorizedError(HTTPError, AuthenticationError):
    """HTTP 401 Unauthorized"""

    def __init__(self, message: str = "Unauthorized", details: dict[str, Any] | None = None):
        super().__init__(message, 401, details)


class HTTPForbiddenError(HTTPError, AuthorizationError):
    """HTTP 403 Forbidden"""

    def __init__(self, message: str = "Forbidden", details: dict[str, Any] | None = None):
        super().__init__(message, 403, details)


class HTTPInternalServerError(HTTPError):
    """HTTP 500 Internal Server Error"""

    def __init__(
        self, message: str = "Internal Server Error", details: dict[str, Any] | None = None
    ):
        super().__init__(message, 500, details)


class WebSocketError(ServiceError):
    """Base WebSocket error"""

    pass


class WebSocketConnectionError(WebSocketError, ConnectionError):
    """WebSocket connection failed"""

    pass


class WebSocketHandlerError(WebSocketError, HandlerError):
    """WebSocket handler error"""

    pass


__all__ = [
    "HTTPError",
    "HTTPNotFoundError",
    "HTTPBadRequestError",
    "HTTPUnauthorizedError",
    "HTTPForbiddenError",
    "HTTPInternalServerError",
    "WebSocketError",
    "WebSocketConnectionError",
    "WebSocketHandlerError",
]
