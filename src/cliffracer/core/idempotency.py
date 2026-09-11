"""Native idempotency key generation, context, and publish integration.

Provides deterministic key extraction, domain payload hashing, and ambient
IdempotencyContext for JetStream message deduplication via Nats-Msg-Id headers.
"""

import contextvars
import functools
import hashlib
import inspect
import json
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from .exceptions import IdempotencyKeyError

# Ambient contextvar for tracking idempotency key across async tasks
idempotency_key_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "idempotency_key", default=None
)

F = TypeVar("F", bound=Callable[..., Any])


class IdempotencyContext:
    """Ambient idempotency key storage backed by contextvars."""

    @staticmethod
    def get() -> str | None:
        """Return the active idempotency key in the current async context."""
        return idempotency_key_var.get()

    @staticmethod
    def set(key: str | None) -> contextvars.Token[str | None]:
        """Set the idempotency key in the current context, returning a restore token."""
        return idempotency_key_var.set(key)

    @staticmethod
    def reset(token: contextvars.Token[str | None]) -> None:
        """Reset the idempotency key to its state prior to set()."""
        idempotency_key_var.reset(token)

    @staticmethod
    def clear() -> None:
        """Clear the ambient idempotency key."""
        idempotency_key_var.set(None)


def compute_payload_hash(payload: Any, algorithm: str = "sha256") -> str:
    """Compute a deterministic hash of a domain payload.

    Excludes dynamic envelope fields (timestamp, correlation_id, source_service)
    to ensure identical retried publishes yield the same hash.
    """
    if isinstance(payload, BaseModel):
        data = {
            k: v
            for k, v in payload.model_dump(mode="json").items()
            if k not in ("timestamp", "source_service", "correlation_id")
        }
    elif isinstance(payload, dict):
        data = {
            k: (v.model_dump(mode="json") if isinstance(v, BaseModel) else v)
            for k, v in payload.items()
            if k not in ("timestamp", "source_service", "correlation_id")
        }
    else:
        data = payload

    try:
        serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        serialized = str(data)

    h = hashlib.new(algorithm)
    h.update(serialized.encode("utf-8"))
    return h.hexdigest()


def format_nats_msg_id(subject: str, key: str, hash_payload: bool = False) -> str:
    """Format a subject-scoped Nats-Msg-Id header value.

    Scope format: f"{subject}:{key}".
    If hash_payload is True, or key length > 128 bytes, or total length > 128 bytes,
    a SHA-256 hash is used to guarantee bounded header size and collision resistance.
    """
    # If key is already subject-scoped
    if key.startswith(f"{subject}:"):
        scoped = key
    else:
        if len(key) > 128 or hash_payload:
            key_part = hashlib.sha256(key.encode("utf-8")).hexdigest()
        else:
            key_part = key
        scoped = f"{subject}:{key_part}"

    if len(scoped) > 128:
        return hashlib.sha256(scoped.encode("utf-8")).hexdigest()

    return scoped


def _extract_key_from_args(
    key: str | Callable[..., str] | None,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    hash_payload: bool,
) -> str:
    """Extract an idempotency key or compute payload hash from function arguments."""
    if callable(key) and not isinstance(key, str):
        extracted_from_fn = key(*args, **kwargs)
        if extracted_from_fn is None:
            raise IdempotencyKeyError("Idempotency key callable returned None")
        return str(extracted_from_fn)

    # Bind arguments using function signature
    sig = inspect.signature(func)
    try:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
    except TypeError as e:
        raise IdempotencyKeyError(f"Failed to bind arguments for {func.__name__}: {e}") from e

    arguments = bound.arguments

    if isinstance(key, str):
        parts = key.split(".")
        current_obj: Any = arguments.get(parts[0])
        for part in parts[1:]:
            if current_obj is None:
                break
            if isinstance(current_obj, dict):
                current_obj = current_obj.get(part)
            elif hasattr(current_obj, part):
                current_obj = getattr(current_obj, part)
            else:
                current_obj = None
                break

        if current_obj is not None:
            if hash_payload:
                return compute_payload_hash(current_obj)
            return str(current_obj)

        if not hash_payload:
            raise IdempotencyKeyError(
                f"Idempotency key {key!r} not found in arguments of {func.__name__}"
            )

    if hash_payload:
        # Domain arguments only; exclude self / cls
        domain_args = {k: v for k, v in arguments.items() if k not in ("self", "cls")}
        return compute_payload_hash(domain_args)

    raise IdempotencyKeyError(
        f"Cannot determine idempotency key for {func.__name__}: specify key or hash_payload=True"
    )


def idempotent(
    func: Any = None,
    *,
    key: str | Callable[..., str] | None = None,
    hash_payload: bool = False,
) -> Any:
    """Decorator marking a handler or service method for idempotent publishing.

    Extracts a domain key from arguments or computes a canonical payload hash,
    binding it to IdempotencyContext for outgoing JetStream publishes.

    Args:
        func: Optional function when used bare as `@idempotent`.
        key: Parameter name (e.g. "order_id"), dotted path (e.g. "order.id"),
            or callable extracting key from (*args, **kwargs).
        hash_payload: If True, hashes the domain arguments/payload using SHA-256.

    Example:
        @idempotent(key="order_id")
        async def handle_order(self, order_id: str, amount: float):
            await self.publish_event("order.processed", order_id=order_id)
    """
    # If key was passed positionally, e.g. @idempotent("order_id")
    if isinstance(func, str):
        key = func
        func = None

    def decorator(target_func: F) -> F:
        effective_key = key
        effective_hash = hash_payload
        # If bare @idempotent was used, default to hash_payload=True
        if effective_key is None and not effective_hash:
            effective_hash = True

        if inspect.iscoroutinefunction(target_func):

            @functools.wraps(target_func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                extracted = _extract_key_from_args(
                    effective_key, target_func, args, kwargs, effective_hash
                )
                token = IdempotencyContext.set(extracted)
                try:
                    return await target_func(*args, **kwargs)
                finally:
                    IdempotencyContext.reset(token)

            async_wrapper._cliffracer_idempotent = True  # type: ignore[attr-defined]
            async_wrapper._cliffracer_idempotency_key = effective_key  # type: ignore[attr-defined]
            async_wrapper._cliffracer_hash_payload = effective_hash  # type: ignore[attr-defined]
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(target_func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            extracted = _extract_key_from_args(
                effective_key, target_func, args, kwargs, effective_hash
            )
            token = IdempotencyContext.set(extracted)
            try:
                return target_func(*args, **kwargs)
            finally:
                IdempotencyContext.reset(token)

        sync_wrapper._cliffracer_idempotent = True  # type: ignore[attr-defined]
        sync_wrapper._cliffracer_idempotency_key = effective_key  # type: ignore[attr-defined]
        sync_wrapper._cliffracer_hash_payload = effective_hash  # type: ignore[attr-defined]
        return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator
