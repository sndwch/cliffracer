"""Core exception hierarchy for service lifecycle, dispatch, and validation."""

from typing import Any


class CliffracerError(Exception):
    """Base exception for all Cliffracer errors"""

    def __init__(self, message: str, details: dict[str, Any] | list[Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] | list[Any] = details if details is not None else {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} - Details: {self.details}"
        return self.message


class ServiceError(CliffracerError):
    """Base exception for service-related errors"""

    pass


class ServiceLifecycleError(ServiceError, RuntimeError):
    """The exception raised on invalid lifecycle state transitions.

    Raised when ``start()`` is invoked or awakens after a service stop has been requested,
    or when lifecycle operations violate state-machine invariants.

    Invariants:
    - Inherits from both ``ServiceError`` and ``RuntimeError`` for compatibility with generic runtime exception handling.
    - Preserves error message and optional detail mappings.
    """

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


class IdempotencyKeyError(HandlerError):
    """The exception raised when an idempotency key cannot be extracted or generated."""

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


# Unified RPC Exception Hierarchy
class RpcError(CliffracerError):
    """Base exception for all RPC failures across both clients and services."""

    pass


class RpcClientError(RpcError):
    """Base exception for client-side invocation, validation, or transport failures."""

    pass


class RpcTimeoutError(RpcClientError, TimeoutError):
    """No reply was received within the client request timeout."""

    pass


class RpcNoRespondersError(RpcClientError):
    """Raised when the broker has no active subscriptions/responders for the target subject."""

    pass


class RpcValidationError(RpcClientError):
    """The service rejected the arguments due to schema validation failure.

    Attributes:
        details: List of Pydantic error dictionaries.
    """

    def __init__(
        self,
        details: list[dict[str, Any]] | None = None,
        message: str = "validation failed",
    ) -> None:
        super().__init__(message, details=details or [])
        self.details: list[dict[str, Any]] = details or []


class RpcUnknownMethodError(RpcClientError):
    """The target service is running and answered, but has no matching RPC method."""

    pass


class RpcRefusedError(RpcClientError):
    """An extension or policy refused the message before the handler ran.

    Attributes:
        reason: Explanation of why the request was refused.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"refused: {reason}")
        self.reason = reason


class ClientOutOfDateError(RpcClientError):
    """The service signatures moved.

    Attributes:
        service: Name of the target service.
        changed: List of method names with mismatched signature hashes.
        missing: List of method names present in client but absent on service.
    """

    def __init__(self, service: str, changed: list[str], missing: list[str]) -> None:
        super().__init__(
            f"client for {service!r} is out of date: "
            f"changed={changed} missing={missing}; regenerate it"
        )
        self.service = service
        self.changed = changed
        self.missing = missing


class RpcServerError(RpcError):
    """Remote service raised an unhandled exception or returned an error envelope."""

    pass


# Backward-compatibility aliases
RpcRemoteError = RpcServerError
ClientError = RpcClientError
RPCError = RpcError
RPCTimeoutError = RpcTimeoutError
RpcTimeout = RpcTimeoutError
RpcNoResponders = RpcNoRespondersError
RpcUnknownMethod = RpcUnknownMethodError
RpcRefused = RpcRefusedError
ClientOutOfDate = ClientOutOfDateError


# Timer-specific errors
class TimerError(ServiceError):
    """Base timer error"""

    pass


# Utility functions for error handling
def wrap_exception(
    original_exception: Exception,
    new_exception_class: type[CliffracerError],
    message: str | None = None,
    details: dict[str, Any] | None = None,
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
        "args": original_exception.args,
    }

    wrapped = new_exception_class(error_message, error_details)
    wrapped.__cause__ = original_exception
    return wrapped


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
        reraise: bool = True,
    ) -> None:
        self.operation_description = operation_description
        self.exception_class = exception_class
        self.details = details or {}
        self.reraise = reraise

    async def __aenter__(self) -> "ErrorHandler":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if exc_type is None or not isinstance(exc_val, Exception):
            return False

        # Don't handle Cliffracer exceptions (already properly typed)
        if isinstance(exc_val, CliffracerError):
            return False

        # Wrap external exceptions
        wrapped_exception = wrap_exception(
            exc_val, self.exception_class, self.operation_description, self.details
        )

        if self.reraise:
            raise wrapped_exception from exc_val

        return True  # Suppress the exception

    def __enter__(self) -> "ErrorHandler":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if exc_type is None or not isinstance(exc_val, Exception):
            return False

        if isinstance(exc_val, CliffracerError):
            return False

        wrapped_exception = wrap_exception(
            exc_val, self.exception_class, self.operation_description, self.details
        )

        if self.reraise:
            raise wrapped_exception from exc_val

        return True
