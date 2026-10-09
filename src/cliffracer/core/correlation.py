"""Correlation ID propagation and context management.

Stores the active correlation ID in a contextvar for propagation across
async dispatch tasks, outgoing NATS messages, and HTTP requests.
"""

import contextvars
import functools
import inspect
import re
import uuid
from typing import Any

from loguru import logger

# An id is logged on every line of its request and copied onto every message the handler sends, and
# it comes from a header any publisher can set. So it is bounded: printable text of at most this many
# characters. A UUID is 36 and a W3C traceparent 55; opaque formats and unicode letters are fine.
MAX_CORRELATION_ID_LENGTH = 256

# Kept for code that imports it. The rule is `refusal_of`, which this pattern is a part of.
INVALID_ID_PATTERN = re.compile(r"[\r\n]")


def refusal_of(correlation_id: str) -> str | None:
    """Why `correlation_id` cannot be an id, or None when it can.

    A control character is refused, not only CR and LF: an ANSI escape rewrites earlier lines of a
    terminal, and vertical tab, form feed, NEL and U+2028 are line breaks to `str.splitlines` and
    to many log collectors, so a logged id holding one reads as several records. `isprintable`
    refuses all of them and allows a space and any printable character of any script. The length
    is bounded because a header the broker accepts can be as large as `max_payload`, and the id
    is re-sent in every header of every message the handler publishes.
    """
    if len(correlation_id) > MAX_CORRELATION_ID_LENGTH:
        return f"is {len(correlation_id)} characters, over the limit of {MAX_CORRELATION_ID_LENGTH}"
    if not correlation_id.isprintable():
        return "holds a control character or a line separator"
    return None


def _shown(correlation_id: str) -> str:
    """The id as a log line may carry it: escaped, and cut to a length that cannot flood a line."""
    return repr(correlation_id[:64]) + ("..." if len(correlation_id) > 64 else "")


def _usable(correlation_id: Any) -> bool:
    """Whether `correlation_id` is a string that may become the id of a message.

    An id comes from a header, which is always text, or from a payload field, which is any JSON
    value. A value that is not a string is not an id, and a string that `refusal_of` refuses is not
    one either; each is treated as absent: the next source is tried, or a new id is made.
    """
    if not isinstance(correlation_id, str):
        logger.warning(
            f"Ignored a correlation ID that is a {type(correlation_id).__name__}, not a string"
        )
        return False
    if (refusal := refusal_of(correlation_id)) is not None:
        logger.warning(f"Rejected invalid correlation ID {_shown(correlation_id)}: it {refusal}")
        return False
    return True


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
    def ambient_for_send() -> str | None:
        """The ambient id, when a message may carry it; None when there is none or it is refused.

        `set()` stores what it is given, so the ambient id is checked where it is read for a send,
        by the rule an inbound id is held to. A refused one is logged and treated as absent.
        """
        existing_id = correlation_id_var.get()
        if not existing_id:
            return None
        if (refusal := refusal_of(existing_id)) is None:
            return existing_id
        logger.warning(
            f"Rejected ambient invalid correlation ID {_shown(existing_id)}: it {refusal}"
        )
        return None

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
        if correlation_id and _usable(correlation_id):
            return correlation_id

        # Check if we already have one in context
        if (existing_id := CorrelationContext.ambient_for_send()) is not None:
            return existing_id

        # Generate new ID
        return f"corr_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def for_message(headers: dict[str, Any] | None, payload: Any, given: str | None = None) -> str:
        """The correlation id of one inbound message: the one given, else the one its headers
        carry, else the one in its payload, else a new one. Never the ambient context.

        A message that reaches several handlers is one unit of work, so the id is resolved once,
        here, and every handler's dispatch carries it.
        """
        found = (
            given
            or CorrelationContext.extract_from_headers(headers or {})
            or (payload.get("correlation_id") if isinstance(payload, dict) else None)
        )
        return CorrelationContext.new_id_unless_given(found)

    @staticmethod
    def new_id_unless_given(correlation_id: str | None = None) -> str:
        """Return the given ID or generate a new one, without reading ambient context.

        For message dispatch, where each message represents an independent unit of work.
        Does not mutate the context variable; dispatch paths set and reset context
        around handler execution.
        """
        if correlation_id and _usable(correlation_id):
            return correlation_id
        return f"corr_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def get() -> str | None:
        """Get current correlation ID from context"""
        return correlation_id_var.get()

    @staticmethod
    def set(correlation_id: str | None) -> contextvars.Token[str | None]:
        """Set correlation ID in current context"""
        return correlation_id_var.set(correlation_id)

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
                    if (refusal := refusal_of(val_str)) is None:
                        return val_str
                    logger.warning(
                        f"Rejected invalid correlation ID from headers {_shown(val_str)}: it {refusal}"
                    )

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


def _bind_correlation_id(
    sig: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[str, tuple[Any, ...], dict[str, Any]]:
    """The id a decorated call runs under, and the arguments that carry it.

    An id the caller passed, by keyword or by position, wins. Otherwise it is
    read from the headers of a request-like argument or from a dict argument's
    ``correlation_id`` key, scanning from the last argument so the request is
    reached before ``self``. A function that takes ``correlation_id`` gets the
    id in that parameter wherever the caller put it, so it is never bound twice.
    """
    try:
        bound: inspect.BoundArguments | None = sig.bind_partial(*args, **kwargs)
    except TypeError:
        # The call does not fit the signature. Its arguments go through as they
        # came, so the function raises Python's own error for them.
        bound = None

    correlation_id = kwargs.get("correlation_id")
    if not correlation_id and bound is not None:
        correlation_id = bound.arguments.get("correlation_id")

    if not correlation_id:
        for arg in reversed(args):
            if hasattr(arg, "headers"):
                correlation_id = CorrelationContext.extract_from_headers(arg.headers)
                if correlation_id:
                    break
            elif isinstance(arg, dict) and "correlation_id" in arg:
                correlation_id = arg["correlation_id"]
                if correlation_id:
                    break

    correlation_id = CorrelationContext.get_or_create_id(correlation_id)

    if bound is not None and "correlation_id" in sig.parameters:
        bound.arguments["correlation_id"] = correlation_id
        return correlation_id, bound.args, bound.kwargs
    return correlation_id, args, kwargs


def with_correlation_id(func: Any) -> Any:
    """Wrap a coroutine or function to establish an active correlation ID context."""

    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        correlation_id, args, kwargs = _bind_correlation_id(inspect.signature(func), args, kwargs)
        token = CorrelationContext.set(correlation_id)
        try:
            return await func(*args, **kwargs)
        finally:
            correlation_id_var.reset(token)

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        correlation_id, args, kwargs = _bind_correlation_id(inspect.signature(func), args, kwargs)
        token = CorrelationContext.set(correlation_id)
        try:
            return func(*args, **kwargs)
        finally:
            correlation_id_var.reset(token)

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
    cid = CorrelationContext.new_id_unless_given(None)
    CorrelationContext.set(cid)
    return cid
