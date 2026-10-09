"""Core exception hierarchy for service lifecycle, dispatch, and validation."""

import builtins
import inspect
import math
from typing import Any

from loguru import logger


def _rebuilt(cls: type["CliffracerError"], args: tuple[Any, ...], state: dict[str, Any]) -> Any:
    """Rebuild an exception without calling its constructor.

    A subclass builds its message from its own arguments (`ClientOutOfDateError`
    takes a service and two lists, `RpcRefusedError` a reason), so the default
    `cls(*args)` that unpickling runs either raises or feeds the formatted
    message back in as one of those arguments.
    """
    error = cls.__new__(cls)
    Exception.__init__(error, *args)
    error.__dict__.update(state)
    return error


class CliffracerError(Exception):
    """Base exception for all Cliffracer errors.

    Every subclass survives `pickle`, so an error can cross a process boundary
    (`multiprocessing`, `ProcessPoolExecutor`) with its fields intact.
    """

    def __init__(self, message: str, details: dict[str, Any] | list[Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] | list[Any] = details if details is not None else {}

    def __reduce__(self) -> tuple[Any, ...]:
        return (_rebuilt, (type(self), self.args, self.__dict__))

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


class ConfigurationError(ServiceError):
    """Errors related to service configuration"""

    pass


class IdempotencyKeyError(ServiceError):
    """The exception raised when an idempotency key cannot be extracted or generated."""

    pass


class ValidationError(ServiceError):
    """Errors related to schema validation"""

    pass


class TimeoutError(ServiceError, builtins.TimeoutError):
    """Errors related to timeouts.

    A REAL `builtins.TimeoutError`, which is also `asyncio.TimeoutError` on
    3.11+. This class shadows that name inside this module, so without the base
    the line `class RpcTimeoutError(RpcClientError, TimeoutError)` reads exactly
    like it grants the builtin and grants something else -- and since this class
    is not exported, user code writing `except TimeoutError:` binds the builtin
    and silently stopped matching. `_request` wraps `nats.errors.TimeoutError`,
    which IS a builtin `TimeoutError`, so without this the wrapper was strictly
    a regression in what could catch a client timeout.
    """

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

    #: For a streamed reply, the items the caller received before this was raised; None for a
    #: call that is not a stream.
    items: int | None = None


class RpcClientError(RpcError):
    """Base exception for client-side invocation, validation, or transport failures."""

    pass


class RpcTimeoutError(RpcClientError, TimeoutError):
    """No reply was received within the client request timeout."""

    pass


class RpcDeadlineExceededError(RpcTimeoutError):
    """The service cut the handler off at the request's deadline, or did not start it, and said so.

    A reply with code `deadline_exceeded`: the deadline was the budget the caller sent, or the
    service's own `max_rpc_processing_time`, whichever ended first (`set_by` says which). It is a
    `RpcTimeoutError`, so a handler of timeouts still matches; this class says it was the service
    that applied it, where `RpcTimeoutError` alone is the caller's own wait running out.
    """

    def __init__(
        self,
        message: str,
        *,
        budget: float | None = None,
        elapsed: float | None = None,
        set_by: str | None = None,
    ) -> None:
        super().__init__(message)
        #: The seconds the handler was given.
        self.budget = budget
        #: The seconds that had passed when the service cut it off or refused to start it.
        self.elapsed = elapsed
        #: "caller" for the caller's budget, "service" for `max_rpc_processing_time`.
        self.set_by = set_by


class RpcNoRespondersError(RpcClientError):
    """Raised when the broker has no active subscriptions/responders for the target subject."""

    pass


class RpcConnectionError(RpcClientError):
    """The connection to the broker was never opened, or was lost before the reply arrived.

    Distinct from `RpcNoRespondersError`, which means the broker answered and
    nothing was subscribed, and from `RpcTimeoutError`, which means the service
    did not reply in time. Both of those require a connection; this is its
    absence. `nats.errors.ConnectionClosedError` and `StaleConnectionError` from
    a call in flight are raised as this, with the nats error as the cause, by
    `ServiceClient` and by the service's own `call_rpc`. It sits under `RpcClientError` so that a caller already catching
    `RpcClientError` or `RpcError` keeps catching a failure that previously
    escaped as a raw `nats.errors` class.
    """

    pass


class RpcValidationError(RpcClientError):
    """The arguments failed schema validation, at either end.

    Raised by the CLIENT when an argument does not match its declared
    annotation, before anything is sent, and by the client again when the
    SERVICE answers that it rejected the payload. One class because the
    caller's remedy is the same either way -- the argument was wrong -- so a
    call site handling one should not have to learn to handle the other.

    Which end refused it is in the message: the local refusal says so, and
    names the declared type. `details` carries pydantic's own errors in both
    cases.

    Attributes:
        details: List of Pydantic error dictionaries.
    """

    def __init__(
        self,
        details: list[dict[str, Any]] | None = None,
        message: str = "validation failed",
    ) -> None:
        # `details` comes first here and `message` first everywhere else in the hierarchy, so
        # `RpcValidationError("username is required")` is easy to write: it used to build a
        # "validation failed" error with that text as its details. Refused instead.
        if details is not None and not (
            isinstance(details, list) and all(isinstance(entry, dict) for entry in details)
        ):
            raise TypeError(
                f"RpcValidationError details must be a list of error dicts, got "
                f"{type(details).__name__}; the message is the second argument (message=...)"
            )
        super().__init__(message, details=details or [])
        self.details: list[dict[str, Any]] = details or []


class RpcUnknownMethodError(RpcClientError):
    """The target service is running and answered, but has no matching RPC method."""

    pass


class RpcRefusedError(RpcClientError):
    """An extension or policy refused the message before the handler ran.

    A `RpcClientError` because a refusal is something the CALLER acts on. An
    extension whose hook crashes also stops the handler running, but that is the
    service being broken and arrives as `RpcServerError`: the dispatcher labels
    the two apart rather than leaving them both wearing this class.

    Attributes:
        reason: Explanation of why the request was refused.
        retry_after: Seconds the service asked the caller to wait before trying again, or `None`.
            A refusal that is a `RetryMessage` (the rate limiter's is) carries it.
        details: What the refusal said beyond its reason, a dict that is empty when it said nothing.
    """

    def __init__(
        self,
        reason: str,
        *,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"refused: {reason}", details=details)
        self.reason = reason
        self.retry_after = retry_after


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


class RpcStreamGapError(RpcError):
    """A streamed reply missed an item: one arrived out of order, or the stream ended having
    sent more than arrived. The broker drops messages a slow subscriber has not taken, so a
    gap is a lost item rather than an error the service reported.

    Attributes:
        expected: The item index, or for the end of the stream the count, that should have come.
        got: The index that came instead, or the items received at the end.
        items: The items received before the gap.
    """

    def __init__(self, subject: str, *, expected: int | str, got: int | str, items: int) -> None:
        super().__init__(
            f"the stream from {subject} lost an item: expected {expected}, got {got}, after "
            f"{items} items"
        )
        self.expected = expected
        self.got = got
        self.items = items


class RpcServerError(RpcError):
    """Remote service raised an unhandled exception or returned an error envelope."""

    pass


class RpcBusyError(RpcServerError):
    """The service did not take the request, and said why: code `busy`.

    It had as many requests in flight as it admits (`limit` and `in_flight` say how many), or it
    was stopping when the request's turn came (both are None then). The handler did not run, so a
    retry, here or on another replica, is safe for the request itself.
    """

    def __init__(
        self, message: str, *, limit: int | None = None, in_flight: int | None = None
    ) -> None:
        super().__init__(message)
        #: The most requests the service admits at once, or None when it was stopping.
        self.limit = limit
        #: How many it had in flight when it refused, or None when it was stopping.
        self.in_flight = in_flight


# Backward-compatibility aliases
def wire_details(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The error entries a service's validation reply carries, or none if it is not that shape.

    They come off the wire, so a reply that holds anything but a list of objects is read as a
    reply without details rather than refused by `RpcValidationError`.
    """
    details = data.get("details")
    if not isinstance(details, list):
        return []
    return [entry for entry in details if isinstance(entry, dict)]


def _wire_retry_after(data: dict[str, Any]) -> float | None:
    """The seconds a refusal reply asks the caller to wait, or none if it is not a usable number."""
    value = data.get("retry_after")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _wire_refusal_details(data: dict[str, Any]) -> dict[str, Any] | None:
    """The object a refusal reply carries as `details`, or none if it carries anything else."""
    details = data.get("details")
    return details if isinstance(details, dict) and details else None


def _wire_seconds(value: Any) -> float | None:
    """A number of seconds a reply carries, or None when it carries anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _wire_count(value: Any) -> int | None:
    """A count a reply carries, or None when it carries anything else."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def raise_for_error_envelope(data: dict[str, Any], subject: str) -> None:
    """Turn an RPC error envelope into the corresponding exception, or return.

    The one place the envelope is read, for the standalone `ServiceClient` and for the service's
    own `call_rpc`, so a caller catches the same class whichever it called through.

    The three recognised shapes are things the CALLER can act on: fix the arguments, call a method
    that exists, stop being refused. They are `RpcClientError`s. Everything else in an error
    envelope is the service reporting its own fault -- the dispatcher answers an unhandled handler
    exception with `Internal server error (correlation_id: ...)`, which matches none of the
    prefixes and lands on the fall-through -- so the fall-through is an `RpcServerError`.
    """
    if "error" not in data:
        return
    err = str(data["error"])
    code = data.get("code")
    if code is not None:
        # A typed field, per ADR-0011. The prose below is what the service happened to format, and
        # with `expose_internal_errors` on it is a handler's own `str(e)` -- so a crash reading
        # "refused: ..." used to reach the caller as a policy refusal, and rewording any of these
        # strings silently reclassified every error in the fleet.
        if code == "validation_failed":
            raise RpcValidationError(wire_details(data))
        if code == "unknown_method":
            raise RpcUnknownMethodError(err)
        if code == "busy":
            raise RpcBusyError(
                f"{subject}: {err}",
                limit=_wire_count(data.get("limit")),
                in_flight=_wire_count(data.get("in_flight")),
            )
        if code == "deadline_exceeded":
            raise RpcDeadlineExceededError(
                f"{subject}: {err}",
                budget=_wire_seconds(data.get("budget")),
                elapsed=_wire_seconds(data.get("elapsed")),
                set_by=data["set_by"] if data.get("set_by") in ("caller", "service") else None,
            )
        if code == "refused":
            raise RpcRefusedError(
                err.removeprefix("refused: "),
                retry_after=_wire_retry_after(data),
                details=_wire_refusal_details(data),
            )
        # Including a code this caller does not know: a newer service naming a case that did not
        # exist here is a remote fault, not a reason to fall through to matching its prose.
        raise RpcServerError(f"{subject}: {err}")

    # No code: a service that has not been redeployed. It keeps exactly the classification it had,
    # which is why this is a prefix match and why it is still spoofable against an OLD service. A
    # new caller cannot fix a message an old service never labelled.
    if data.get("success") is False and err == "validation failed":
        raise RpcValidationError(wire_details(data))
    if err.startswith("Unknown method:"):
        raise RpcUnknownMethodError(err)
    if err.startswith("refused: "):
        raise RpcRefusedError(err[len("refused: ") :])
    raise RpcServerError(f"{subject}: {err}")


RpcRemoteError = RpcServerError
ClientError = RpcClientError
RPCError = RpcError
RPCTimeoutError = RpcTimeoutError
RpcTimeout = RpcTimeoutError
RpcNoResponders = RpcNoRespondersError
RpcUnknownMethod = RpcUnknownMethodError
RpcRefused = RpcRefusedError
ClientOutOfDate = ClientOutOfDateError


# Utility functions for error handling
def _takes_message_and_details(exception_class: type[CliffracerError]) -> bool:
    """Whether ``exception_class`` is built as ``exception_class(message, details)``.

    Most of the hierarchy is. A few classes build their message from their own
    arguments (``RpcRefusedError(reason)``, ``ClientOutOfDateError(service, ...)``)
    or take the same two arguments in the other order (``RpcValidationError``),
    and calling them positionally either raises or fills the wrong fields.
    """
    positional = [
        parameter.name
        for parameter in inspect.signature(exception_class).parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return positional[:2] == ["message", "details"]


def _require_a_message_and_details_class(exception_class: type[CliffracerError]) -> None:
    if not _takes_message_and_details(exception_class):
        raise ConfigurationError(
            f"{exception_class.__name__} cannot wrap an exception: its constructor is "
            f"{inspect.signature(exception_class)}, not (message, details)"
        )


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
        New Cliffracer exception. Its `details` hold the type and message of the
        original under `original_exception`; the original itself is
        `__cause__`, with its arguments.

    Raises:
        ConfigurationError: ``new_exception_class`` is not built from a message
            and details, so it cannot carry them.
    """
    _require_a_message_and_details_class(new_exception_class)
    error_message = message or str(original_exception)
    # A copy: the caller's mapping is theirs, and this one becomes the new
    # exception's own record of its cause.
    error_details = dict(details) if details else {}
    error_details["original_exception"] = {
        "type": type(original_exception).__name__,
        "message": str(original_exception),
    }

    wrapped = new_exception_class(error_message, error_details)
    wrapped.__cause__ = original_exception
    return wrapped


# Context manager for error handling
class ErrorHandler:
    """
    Context manager for consistent error handling in services.

    A failure that is not already a `CliffracerError` is wrapped in
    `exception_class` with `operation_description` as its message, and raised
    from the original. With `reraise=False` it is not raised: the block carries
    on past the failure, and the failure is logged at WARNING with its type and
    message, so nothing is swallowed silently.

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
        _require_a_message_and_details_class(exception_class)
        self.operation_description = operation_description
        self.exception_class = exception_class
        self.details = details or {}
        self.reraise = reraise

    def _handle(self, exc_type: type[BaseException] | None, exc_val: BaseException | None) -> bool:
        """Whether the block's failure is suppressed; raises the wrapped one otherwise."""
        if exc_type is None or not isinstance(exc_val, Exception):
            return False

        # Don't handle Cliffracer exceptions (already properly typed)
        if isinstance(exc_val, CliffracerError):
            return False

        wrapped_exception = wrap_exception(
            exc_val, self.exception_class, self.operation_description, self.details
        )

        if self.reraise:
            raise wrapped_exception from exc_val

        logger.warning(
            "{} failed and was suppressed: {}: {}",
            self.operation_description,
            type(exc_val).__name__,
            exc_val,
        )
        return True

    async def __aenter__(self) -> "ErrorHandler":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        return self._handle(exc_type, exc_val)

    def __enter__(self) -> "ErrorHandler":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        return self._handle(exc_type, exc_val)
