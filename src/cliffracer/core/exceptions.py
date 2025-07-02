"""
Unified exception hierarchy for Cliffracer

This module provides a comprehensive exception hierarchy for consistent
error handling throughout the framework.
"""

from typing import Any


class CliffracerError(Exception):
    """Base exception for all Cliffracer errors"""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} - Details: {self.details}"
        return self.message


class ServiceError(CliffracerError):
    """Base exception for service-related errors"""
    pass


class ConnectionError(ServiceError):
    """Errors related to NATS or other connections"""
    pass


class ConfigurationError(ServiceError):
    """Errors related to service configuration"""
    pass


class HandlerError(ServiceError):
    """Errors related to RPC/event handlers"""
    pass


class ValidationError(ServiceError):
    """Errors related to schema validation"""
    pass


class TimeoutError(ServiceError):
    """Errors related to timeouts"""
    pass


class AuthenticationError(ServiceError):
    """Errors related to authentication"""
    pass


class AuthorizationError(ServiceError):
    """Errors related to authorization"""
    pass


# NATS-specific errors
class NATSConnectionError(ConnectionError):
    """NATS connection failed"""
    pass


class NATSSubscriptionError(ConnectionError):
    """NATS subscription failed"""
    pass


class NATSPublishError(ConnectionError):
    """NATS publish operation failed"""
    pass


# RPC-specific errors
class RPCError(HandlerError):
    """Base RPC error"""
    pass


class RPCTimeoutError(RPCError, TimeoutError):
    """RPC call timed out"""
    pass


class RPCMethodNotFoundError(RPCError):
    """RPC method not found"""
    pass


class RPCValidationError(RPCError, ValidationError):
    """RPC request validation failed"""
    pass


# HTTP-specific errors
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

    def __init__(self, message: str = "Internal Server Error", details: dict[str, Any] | None = None):
        super().__init__(message, 500, details)


# WebSocket-specific errors
class WebSocketError(ServiceError):
    """Base WebSocket error"""
    pass


class WebSocketConnectionError(WebSocketError, ConnectionError):
    """WebSocket connection failed"""
    pass


class WebSocketHandlerError(WebSocketError, HandlerError):
    """WebSocket handler error"""
    pass


# Timer-specific errors
class TimerError(ServiceError):
    """Base timer error"""
    pass


class TimerExecutionError(TimerError):
    """Timer method execution failed"""
    pass


class TimerConfigurationError(TimerError, ConfigurationError):
    """Timer configuration invalid"""
    pass


# Database-specific errors
class DatabaseError(ServiceError):
    """Base database error"""
    pass


class DatabaseConnectionError(DatabaseError, ConnectionError):
    """Database connection failed"""
    pass


class DatabaseQueryError(DatabaseError):
    """Database query failed"""
    pass


class DatabaseTransactionError(DatabaseError):
    """Database transaction failed"""
    pass


# Performance-specific errors
class PerformanceError(ServiceError):
    """Base performance error"""
    pass


class ConnectionPoolError(PerformanceError):
    """Connection pool error"""
    pass


class BatchProcessingError(PerformanceError):
    """Batch processing error"""
    pass


class MetricsError(PerformanceError):
    """Metrics collection error"""
    pass


# Monitoring-specific errors
class MonitoringError(ServiceError):
    """Base monitoring error"""
    pass


class MetricsCollectionError(MonitoringError):
    """Metrics collection failed"""
    pass


class AlertingError(MonitoringError):
    """Alerting system error"""
    pass


# Utility functions for error handling
def wrap_exception(
    original_exception: Exception,
    new_exception_class: type[CliffracerError],
    message: str | None = None,
    details: dict[str, Any] | None = None
) -> CliffracerError:
    """
    Wrap an external exception in a Cliffracer exception.

    Args:
        original_exception: The original exception to wrap
        new_exception_class: The Cliffracer exception class to use
        message: Optional custom message (uses original message if not provided)
        details: Additional details to include

    Returns:
        New Cliffracer exception with original exception details
    """
    error_message = message or str(original_exception)
    error_details = details or {}
    error_details["original_exception"] = {
        "type": type(original_exception).__name__,
        "message": str(original_exception),
        "args": original_exception.args
    }

    wrapped = new_exception_class(error_message, error_details)
    wrapped.__cause__ = original_exception
    return wrapped


def handle_nats_error(exception: Exception) -> CliffracerError:
    """Convert NATS-related exceptions to Cliffracer exceptions"""
    from nats.errors import Error as NATSError
    from nats.errors import TimeoutError as NATSTimeoutError

    if isinstance(exception, NATSTimeoutError):
        return wrap_exception(exception, RPCTimeoutError, "NATS operation timed out")
    elif isinstance(exception, NATSError):
        return wrap_exception(exception, NATSConnectionError, "NATS error occurred")
    else:
        return wrap_exception(exception, ConnectionError, "Connection error")


def handle_validation_error(exception: Exception) -> CliffracerError:
    """Convert validation exceptions to Cliffracer exceptions"""
    from pydantic import ValidationError as PydanticValidationError

    if isinstance(exception, PydanticValidationError):
        details = {"validation_errors": exception.errors()}
        return ValidationError("Request validation failed", details)
    else:
        return wrap_exception(exception, ValidationError, "Validation error")


def handle_database_error(exception: Exception) -> CliffracerError:
    """Convert database exceptions to Cliffracer exceptions"""
    import asyncpg

    if isinstance(exception, asyncpg.PostgresError):
        details = {
            "sqlstate": getattr(exception, 'sqlstate', None),
            "detail": getattr(exception, 'detail', None),
            "hint": getattr(exception, 'hint', None)
        }
        return DatabaseQueryError(f"Database error: {exception}", details)
    elif isinstance(exception, asyncpg.ConnectionError):
        return wrap_exception(exception, DatabaseConnectionError, "Database connection failed")
    else:
        return wrap_exception(exception, DatabaseError, "Database operation failed")


def create_error_response(exception: CliffracerError) -> dict[str, Any]:
    """
    Create a standardized error response dictionary.

    Args:
        exception: The Cliffracer exception to convert

    Returns:
        Dictionary suitable for JSON serialization
    """
    from datetime import UTC, datetime

    response = {
        "error": True,
        "error_type": type(exception).__name__,
        "message": exception.message,
        "timestamp": datetime.now(UTC).isoformat()
    }

    if exception.details:
        response["details"] = exception.details

    # Add HTTP status code if available
    if hasattr(exception, 'status_code'):
        response["status_code"] = exception.status_code

    return response


# Context manager for error handling
class ErrorHandler:
    """
    Context manager for consistent error handling in services.

    Example:
        async with ErrorHandler("RPC call failed"):
            result = await some_operation()
    """

    def __init__(
        self,
        operation_description: str,
        exception_class: type[CliffracerError] = ServiceError,
        details: dict[str, Any] | None = None,
        reraise: bool = True
    ):
        self.operation_description = operation_description
        self.exception_class = exception_class
        self.details = details or {}
        self.reraise = reraise

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            return False

        # Don't handle Cliffracer exceptions (already properly typed)
        if isinstance(exc_val, CliffracerError):
            return False

        # Wrap external exceptions
        wrapped_exception = wrap_exception(
            exc_val,
            self.exception_class,
            self.operation_description,
            self.details
        )

        if self.reraise:
            raise wrapped_exception from exc_val

        return True  # Suppress the exception

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            return False

        if isinstance(exc_val, CliffracerError):
            return False

        wrapped_exception = wrap_exception(
            exc_val,
            self.exception_class,
            self.operation_description,
            self.details
        )

        if self.reraise:
            raise wrapped_exception from exc_val

        return True
