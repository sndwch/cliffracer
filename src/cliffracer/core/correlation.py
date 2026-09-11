"""Correlation ID propagation and context management.

Stores the active correlation ID in a contextvar for propagation across
async dispatch tasks, outgoing NATS messages, and HTTP requests.
"""

import contextvars
import uuid
from typing import Any

# Context variable for storing correlation ID in async context
correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


class CorrelationContext:
    """Ambient correlation ID storage backed by contextvars.

    Maintains a per-task correlation ID. Message dispatch uses
    ``new_id_unless_given`` to avoid leaking ambient IDs across message
    callback tasks; request-scoped middleware uses ``get_or_create_id``.
    """

    @staticmethod
    def get_or_create_id(correlation_id: str | None = None) -> str:
        """
        Get existing correlation ID or create a new one.

        For request-scoped contexts (e.g. HTTP middleware) where ambient context
        represents a single request. For message dispatch, use `new_id_unless_given`
        to prevent correlation IDs leaking across long-lived message callback contexts.

        Args:
            correlation_id: Optional ID to use, generates UUID if not provided

        Returns:
            Correlation ID string
        """
        if correlation_id:
            return correlation_id

        # Check if we already have one in context
        existing_id = correlation_id_var.get()
        if existing_id:
            return existing_id

        # Generate new ID
        new_id = f"corr_{uuid.uuid4().hex[:16]}"
        correlation_id_var.set(new_id)
        return new_id

    @staticmethod
    def new_id_unless_given(correlation_id: str | None = None) -> str:
        """Return the given ID or generate a new one, without reading ambient context.

        For message dispatch, where each message represents an independent unit of work.
        Does not mutate the context variable; dispatch paths set and reset context
        around handler execution.
        """
        if correlation_id:
            return correlation_id
        return f"corr_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def get() -> str | None:
        """Get current correlation ID from context"""
        return correlation_id_var.get()

    @staticmethod
    def set(correlation_id: str | None) -> None:
        """Set correlation ID in current context"""
        correlation_id_var.set(correlation_id)

    @staticmethod
    def clear() -> None:
        """Clear correlation ID from context"""
        correlation_id_var.set(None)

    @staticmethod
    def extract_from_headers(headers: dict[str, Any] | None) -> str | None:
        """Extract correlation ID matching standard header names in case-normalized order."""
        if not headers or not isinstance(headers, dict):
            return None

        # Normalize headers to handle case-insensitive lookups
        normalized_headers = {str(k).lower(): v for k, v in headers.items() if v is not None}

        header_names = [
            "x-correlation-id",
            "x-request-id",
            "x-trace-id",
            "correlation-id",
            "correlation_id",
            "request-id",
            "trace-id",
        ]

        for name in header_names:
            value = normalized_headers.get(name)
            if value:
                val_str = str(value).strip()
                if val_str:
                    return val_str

        return None

    @staticmethod
    def inject_into_headers(
        headers: dict[str, Any], correlation_id: str | None = None
    ) -> dict[str, Any]:
        """Set X-Correlation-ID in headers from the given ID or active context."""
        cid = correlation_id or correlation_id_var.get()
        if cid:
            headers["X-Correlation-ID"] = cid
        return headers


def with_correlation_id(func: Any) -> Any:
    """Wrap a coroutine or function to establish an active correlation ID context."""
    import functools
    import inspect

    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        # Try to extract correlation ID from kwargs
        correlation_id = kwargs.get("correlation_id")

        # If not in kwargs, check if first arg is a request-like object
        if not correlation_id and args:
            first_arg = args[0] if len(args) > 1 else None  # Skip self
            if first_arg is not None:
                if hasattr(first_arg, "headers"):
                    correlation_id = CorrelationContext.extract_from_headers(first_arg.headers)
                elif isinstance(first_arg, dict) and "correlation_id" in first_arg:
                    correlation_id = first_arg["correlation_id"]

        # Ensure we have a correlation ID
        correlation_id = CorrelationContext.get_or_create_id(correlation_id)
        CorrelationContext.set(correlation_id)

        # Inject into kwargs if function accepts it
        sig = inspect.signature(func)
        if "correlation_id" in sig.parameters:
            kwargs["correlation_id"] = correlation_id

        try:
            return await func(*args, **kwargs)
        finally:
            # Don't clear - let context naturally expire
            pass

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        correlation_id = kwargs.get("correlation_id")

        if not correlation_id and args:
            first_arg = args[0] if len(args) > 1 else None
            if first_arg is not None:
                if hasattr(first_arg, "headers"):
                    correlation_id = CorrelationContext.extract_from_headers(first_arg.headers)
                elif isinstance(first_arg, dict) and "correlation_id" in first_arg:
                    correlation_id = first_arg["correlation_id"]

        correlation_id = CorrelationContext.get_or_create_id(correlation_id)
        CorrelationContext.set(correlation_id)

        sig = inspect.signature(func)
        if "correlation_id" in sig.parameters:
            kwargs["correlation_id"] = correlation_id

        try:
            return func(*args, **kwargs)
        finally:
            pass

    if inspect.iscoroutinefunction(func):
        return async_wrapper
    else:
        return sync_wrapper


# Convenience functions
def get_correlation_id() -> str | None:
    """Get current correlation ID"""
    return CorrelationContext.get()


def set_correlation_id(correlation_id: str) -> None:
    """Set correlation ID in current context"""
    CorrelationContext.set(correlation_id)


def create_correlation_id() -> str:
    """Create and set a new correlation ID"""
    return CorrelationContext.get_or_create_id()
