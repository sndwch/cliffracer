"""
Correlation ID Propagation System for Cliffracer

This module provides correlation ID tracking across distributed service calls,
enabling request tracing through the entire microservices mesh.
"""

import contextvars
import uuid

from loguru import logger

# Context variable for storing correlation ID in async context
correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    'correlation_id',
    default=None
)


class CorrelationContext:
    """
    Manages correlation IDs for request tracking across services.

    Features:
    - Automatic ID generation if not provided
    - Context-aware storage using contextvars
    - Thread-safe and async-safe
    - Integration with logging and messaging
    """

    @staticmethod
    def get_or_create_id(correlation_id: str | None = None) -> str:
        """
        Get existing correlation ID or create a new one.

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
    def extract_from_headers(headers: dict) -> str | None:
        """
        Extract correlation ID from HTTP headers.

        Checks common header names:
        - X-Correlation-ID
        - X-Request-ID
        - X-Trace-ID
        """
        # Normalize headers to handle case-insensitive lookups
        normalized_headers = {k.lower(): v for k, v in headers.items()}

        header_names = [
            'x-correlation-id',
            'x-request-id',
            'x-trace-id',
            'correlation-id',
            'request-id',
            'trace-id'
        ]

        for name in header_names:
            value = normalized_headers.get(name)
            if value:
                return value

        return None

    @staticmethod
    def inject_into_headers(headers: dict, correlation_id: str | None = None) -> dict:
        """
        Inject correlation ID into HTTP headers.

        Args:
            headers: Existing headers dict
            correlation_id: ID to inject (uses context if not provided)

        Returns:
            Updated headers dict
        """
        cid = correlation_id or correlation_id_var.get()
        if cid:
            headers['X-Correlation-ID'] = cid
        return headers


class CorrelationLoggerAdapter:
    """
    Adapter to automatically include correlation ID in log messages.
    """

    @staticmethod
    def configure_logger():
        """Configure loguru to include correlation ID in logs"""
        def correlation_filter(record):
            """Add correlation ID to log record"""
            correlation_id = correlation_id_var.get()
            record["extra"]["correlation_id"] = correlation_id or "no-correlation"
            return True

        # Add filter to logger
        logger.add(
            filter=correlation_filter,
            format="{time} | {level} | {extra[correlation_id]} | {message}"
        )


def with_correlation_id(func):
    """
    Decorator to ensure a correlation ID exists for the duration of a function.

    Usage:
        @with_correlation_id
        async def my_handler(self, request):
            # Correlation ID is guaranteed to exist here
            pass
    """
    import functools
    import inspect

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        # Try to extract correlation ID from kwargs
        correlation_id = kwargs.get('correlation_id')

        # If not in kwargs, check if first arg is a request-like object
        if not correlation_id and args:
            first_arg = args[0] if len(args) > 1 else None  # Skip self
            if hasattr(first_arg, 'headers'):
                correlation_id = CorrelationContext.extract_from_headers(first_arg.headers)
            elif isinstance(first_arg, dict) and 'correlation_id' in first_arg:
                correlation_id = first_arg['correlation_id']

        # Ensure we have a correlation ID
        correlation_id = CorrelationContext.get_or_create_id(correlation_id)
        CorrelationContext.set(correlation_id)

        # Inject into kwargs if function accepts it
        sig = inspect.signature(func)
        if 'correlation_id' in sig.parameters:
            kwargs['correlation_id'] = correlation_id

        try:
            return await func(*args, **kwargs)
        finally:
            # Don't clear - let context naturally expire
            pass

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        correlation_id = kwargs.get('correlation_id')

        if not correlation_id and args:
            first_arg = args[0] if len(args) > 1 else None
            if hasattr(first_arg, 'headers'):
                correlation_id = CorrelationContext.extract_from_headers(first_arg.headers)
            elif isinstance(first_arg, dict) and 'correlation_id' in first_arg:
                correlation_id = first_arg['correlation_id']

        correlation_id = CorrelationContext.get_or_create_id(correlation_id)
        CorrelationContext.set(correlation_id)

        sig = inspect.signature(func)
        if 'correlation_id' in sig.parameters:
            kwargs['correlation_id'] = correlation_id

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
