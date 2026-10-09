"""Native idempotency key generation, context, and publish integration.

Provides deterministic key extraction, domain payload hashing, and ambient
IdempotencyContext for JetStream message deduplication via Nats-Msg-Id headers.
"""

import contextvars
import dataclasses
import datetime
import decimal
import enum
import functools
import hashlib
import inspect
import json
import pathlib
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, RootModel

from .exceptions import ConfigurationError, IdempotencyKeyError
from .validation import _where_the_dump_fails

# Ambient contextvar for tracking idempotency key across async tasks
idempotency_key_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "idempotency_key", default=None
)

# How many messages this decorated call has already published.
#
# THE KEY IS PER MESSAGE, NOT PER HANDLER. One ambient key for a whole handler
# gave every publish to one subject the same `Nats-Msg-Id`, so JetStream
# dropped all but the first -- a handler emitting `started` then `completed`
# lost `completed`, and `publish_event` returned a normal ack carrying the
# FIRST message's sequence, so the caller could not tell.
#
# A COUNTER RATHER THAN THE PAYLOAD, because the payload does not distinguish
# the case the issue names: one event per line item can legitimately carry
# identical payloads, and hashing them would swallow the duplicates all over
# again. The ordinal separates them.
#
# It is what keeps a RETRY deduplicating, which is the whole point of the
# feature: a retried handler re-runs from the start and publishes the same
# messages in the same order, so message n of attempt 2 gets the id message n
# of attempt 1 had, and JetStream recognises it. That rests on the handler
# publishing deterministically -- the same assumption the ambient key already
# made, now written down. A handler that publishes from concurrent tasks has
# no message order to retry against; see `docs/api-reference.md`.
#
# A list holds the count so that a child task, which copies the context rather
# than sharing it, still increments the invocation's own counter.
idempotency_sequence_var: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "idempotency_sequence", default=None
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

    @staticmethod
    def next_sequence() -> int | None:
        """The ordinal of the next message this decorated call publishes.

        `None` outside a decorated call: a publish with an explicit key and no
        `@idempotent` around it is one message the caller is keying itself, and
        numbering it would change an id the caller chose.
        """
        counter = idempotency_sequence_var.get()
        if counter is None:
            return None
        ordinal = counter[0]
        counter[0] += 1
        return ordinal


# Types whose `str()` is a property of the value and not of this process, so
# hashing it gives the same answer everywhere. They were already handled by the
# old `default=str`, correctly, and are spelled out here so that stays true by
# decision rather than by accident -- and so their existing hashes do not move.
_STABLE_STR_TYPES: tuple[type, ...] = (
    datetime.datetime,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    decimal.Decimal,
    uuid.UUID,
    pathlib.PurePath,
)


def _set_member_key(member: Any) -> str:
    return json.dumps(member, sort_keys=True)


def _sets_in_order(held: Any, dumped: Any) -> Any:
    """`dumped`, a model's JSON-mode dump, with each set the model held put in order.

    The dump writes a `set` or `frozenset` as a list in the order the set iterated, which for
    `str` and `tuple` members follows the process's hash seed. The dump no longer says which
    lists were sets, so this walks what the model held beside it (its declared fields, its
    computed fields and the extra values it keeps), and writes each set as a set
    given directly is (`_encodable`): `{"__set__": [...]}`, its members sorted by their encoded
    form. A list stays as it is: two lists in different orders are different payloads. A value a
    serializer wrote as anything but a list of the set's size is left as it was written."""
    if isinstance(held, RootModel):
        return _sets_in_order(held.root, dumped)
    if isinstance(held, BaseModel):
        if not isinstance(dumped, dict):
            return dumped
        out = dict(dumped)
        cls = type(held)
        # Declared fields, computed fields (read through the property, as the dump read them) and
        # the extra values an `extra="allow"` model keeps, each under the key the dump wrote it.
        held_values: list[tuple[tuple[str | None, ...], Any]] = [
            ((name, field.serialization_alias, field.alias), lambda name=name: getattr(held, name))
            for name, field in cls.model_fields.items()
        ]
        held_values += [
            ((name, field.alias), lambda name=name: getattr(held, name))
            for name, field in cls.model_computed_fields.items()
        ]
        held_values += [
            ((name,), lambda value=value: value)
            for name, value in (held.__pydantic_extra__ or {}).items()
        ]
        for keys, read in held_values:
            for key in keys:
                if key is not None and key in out:
                    out[key] = _sets_in_order(read(), out[key])
                    break
        return out
    if dataclasses.is_dataclass(held) and not isinstance(held, type):
        if not isinstance(dumped, dict):
            return dumped
        return {
            key: _sets_in_order(getattr(held, key, None), each) if hasattr(held, key) else each
            for key, each in dumped.items()
        }
    if isinstance(held, set | frozenset):
        if not isinstance(dumped, list) or len(dumped) != len(held):
            return dumped
        # The dump lists the members in the order the set iterated, so each pairs with its own.
        members = [_sets_in_order(member, each) for member, each in zip(held, dumped, strict=True)]
        return {"__set__": sorted(members, key=_set_member_key)}
    if isinstance(held, list | tuple):
        if not isinstance(dumped, list) or len(dumped) != len(held):
            return dumped
        return [_sets_in_order(member, each) for member, each in zip(held, dumped, strict=True)]
    if isinstance(held, dict):
        if not isinstance(dumped, dict) or len(dumped) != len(held):
            return dumped
        # A dict's dump keeps its insertion order, and a key may be written differently (an int
        # key as a string), so each value pairs with its own by position.
        return {
            key: _sets_in_order(member, each)
            for member, (key, each) in zip(held.values(), dumped.items(), strict=True)
        }
    return dumped


def _model_dump(model: BaseModel, path: str) -> Any:
    """A model's JSON-mode dump, as hashed: each set it holds in order (`_sets_in_order`).

    A computed field runs twice per hash: once for the dump, and once for `_sets_in_order` to see
    what it returned. A dump that fails (bytes that are not UTF-8, an object JSON cannot write, a
    computed field that raises) is refused as any value that cannot be hashed is, by
    `IdempotencyKeyError` naming the field, with the dump's own error as its cause."""
    try:
        dumped = model.model_dump(mode="json")
    except Exception as exc:
        where, holder = _where_the_dump_fails(model, path)
        raise IdempotencyKeyError(
            f"cannot compute a deterministic idempotency key: {where} of {holder} cannot be "
            f"written as JSON ({type(exc).__name__}), so there is nothing to hash. Pass a value "
            f"this can encode, or give @idempotent an explicit key= instead of hashing the "
            f"payload."
        ) from exc
    return _sets_in_order(model, dumped)


def _encodable(value: Any, path: str = "payload") -> Any:
    """A JSON-encodable form of `value` that is identical in every process.

    Raises `IdempotencyKeyError` for anything that cannot be given one. That is
    the whole point: the previous version answered with `default=str` and then
    `str(data)`, which always produced *a* string and so always produced *a*
    key. For an ordinary object that string carries a memory address, so a
    retry from a restarted process computed a different key, JetStream saw an
    id it had not seen, and the duplicate it exists to reject was stored --
    with nothing raised and nothing logged.

    A key that cannot be computed is worth an exception at publish time. A key
    that is wrong is worth nothing at all.
    """
    if isinstance(value, BaseModel):
        return _encodable(_model_dump(value, path), path)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, enum.Enum):
        # `str()` of a member is "Class.NAME" -- a property of the member, not
        # of its value or this process -- so it was already stable and keeping
        # it means an enum in a payload keeps the key it had.
        return str(value)
    if isinstance(value, _STABLE_STR_TYPES):
        return str(value)
    if isinstance(value, bytes):
        # Likewise total and stable; `hex()` would be tidier and would re-key
        # every payload carrying a blob for no gain.
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        # STRUCTURALLY, and this one does re-key -- see the changelog.
        #
        # `str()` of a dataclass is stable only when its fields are, because it
        # interpolates each field's repr:
        #
        #     Holder(thing=<__main__.Opaque object at 0x72b9d3cd5550>)
        #
        # So keeping `str()` would keep the original bug for any dataclass
        # carrying an object, which is the shape a domain payload usually has.
        # Stability here is a property of the VALUE, not of the type, and a
        # rule written per type cannot express it.
        return _encodable(dataclasses.asdict(value), path)
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            # The ordinary case, and it must encode EXACTLY as it did before:
            # `json.dumps(..., sort_keys=True)` already gives string keys one
            # order in every process, so a payload that used to hash correctly
            # keeps the key it had. Changing that would silently re-key every
            # in-flight publish across a deploy.
            return {k: _encodable(v, f"{path}[{k!r}]") for k, v in value.items()}
        # Mixed or non-string keys only. `sort_keys=True` raises on those, and
        # the old fallback then hashed `str(data)`, which preserves INSERTION
        # order -- so two dicts that compare equal hashed differently. Pairs
        # rather than an object, because encoding keys to strings would let
        # 1 and "1" collide.
        pairs = [
            (_encodable(k, f"{path}[key]"), _encodable(v, f"{path}[{k!r}]"))
            for k, v in value.items()
        ]
        return {"__map__": sorted(pairs, key=lambda kv: json.dumps(kv[0], sort_keys=True))}
    if isinstance(value, set | frozenset):
        # Sorted by encoded form. Set iteration order depends on
        # PYTHONHASHSEED for str and tuple members -- measured varying across
        # four seeds -- while int and float members happen to be stable,
        # because small ints hash to themselves. That makes stability a
        # property of the MEMBERS rather than of `set`, so there is no
        # per-type rule to keep: one encoding, and numeric sets re-key. Sorting
        # the members directly would fail on a set of mixed types.
        members = [_encodable(v, f"{path}[member]") for v in value]
        return {"__set__": sorted(members, key=lambda m: json.dumps(m, sort_keys=True))}
    if isinstance(value, list | tuple):
        return [_encodable(v, f"{path}[{i}]") for i, v in enumerate(value)]

    raise IdempotencyKeyError(
        f"cannot compute a deterministic idempotency key: {path} is a "
        f"{type(value).__name__}, which has no encoding that is the same in "
        f"every process. Pass a value this can encode -- a Pydantic model, a "
        f"dataclass, or a plain JSON type -- or give @idempotent an explicit "
        f"key= instead of hashing the payload."
    )


def _canonical_json(data: Any) -> str:
    """The exact bytes hashed. Unchanged for any payload JSON could already encode."""
    return json.dumps(_encodable(data), sort_keys=True, separators=(",", ":"))


def compute_payload_hash(payload: Any, algorithm: str = "sha256") -> str:
    """Compute a deterministic hash of a domain payload.

    Deterministic means the same answer in every process, so a retry from a
    restarted service computes the key its first attempt did. Excludes dynamic
    envelope fields (timestamp, correlation_id, source_service) so an identical
    retried publish hashes the same.

    Raises `IdempotencyKeyError` for a payload containing something that has no
    such encoding, rather than hashing its `repr()`. See `_encodable`.
    """
    if isinstance(payload, BaseModel):
        data = {
            k: v
            for k, v in _model_dump(payload, "payload").items()
            if k not in ("timestamp", "source_service", "correlation_id")
        }
    elif isinstance(payload, dict):
        data = {
            k: (_model_dump(v, f"payload[{k!r}]") if isinstance(v, BaseModel) else v)
            for k, v in payload.items()
            if k not in ("timestamp", "source_service", "correlation_id")
        }
    else:
        data = payload

    serialized = _canonical_json(data)

    h = hashlib.new(algorithm)
    h.update(serialized.encode("utf-8"))
    return h.hexdigest()


def format_nats_msg_id(
    subject: str, key: str, hash_payload: bool = False, sequence: int | None = None
) -> str:
    """Format a subject-scoped Nats-Msg-Id header value.

    Scope format: f"{subject}:{key}", plus f"#{sequence}" when the publish is
    the nth message of a decorated call. Without the ordinal every message a
    handler sent to one subject shared an id and JetStream kept only the first.
    Message 0 carries no suffix, so a handler that publishes once keeps the id
    it had and its in-flight deduplication is not reset by this change.

    If hash_payload is True, or the key is longer than 128 bytes, or the whole value is,
    a SHA-256 hash is used to bound the header size. Lengths are UTF-8 bytes, which is
    what goes on the wire, so a key of non-ASCII characters is measured by its encoding.
    """
    # If key is already subject-scoped
    if key.startswith(f"{subject}:"):
        scoped = key
    else:
        if len(key.encode("utf-8")) > 128 or hash_payload:
            key_part = hashlib.sha256(key.encode("utf-8")).hexdigest()
        else:
            key_part = key
        scoped = f"{subject}:{key_part}"

    # Appended AFTER the length check's input is built but BEFORE the hash, so
    # two messages of one call cannot collapse onto one id by being long.
    if sequence:
        scoped = f"{scoped}#{sequence}"

    if len(scoped.encode("utf-8")) > 128:
        return hashlib.sha256(scoped.encode("utf-8")).hexdigest()

    return scoped


def _resolve_key_path(key: str, arguments: dict[str, Any], func_name: str) -> Any:
    """The value a parameter name or dotted path names, or `IdempotencyKeyError`.

    A missing step and a `None` value are refused separately and named as
    such, because their remedies differ: a missing step is a typo or a renamed
    field, a `None` is data the key cannot be taken from. `None` is never a
    key -- hashing it or `str()`-ing it would put every such message under one
    id.
    """

    def not_found(reason: str) -> IdempotencyKeyError:
        return IdempotencyKeyError(
            f"Idempotency key {key!r} not found in arguments of {func_name}: {reason}"
        )

    first, *rest = key.split(".")
    if first not in arguments:
        raise not_found(f"{first!r} is not a parameter")

    path = first
    value: Any = arguments[first]
    for part in rest:
        if value is None:
            raise not_found(f"{path!r} is None")
        if isinstance(value, dict):
            if part not in value:
                raise not_found(f"{path!r} has no key {part!r}")
            value = value[part]
        elif hasattr(value, part):
            value = getattr(value, part)
        else:
            raise not_found(f"{path!r} has no attribute {part!r}")
        path = f"{path}.{part}"

    if value is None:
        raise IdempotencyKeyError(
            f"Idempotency key {key!r} is None in the call to {func_name}; "
            f"None cannot identify a message"
        )
    return value


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
        # `hash_payload` hashes the value the key finds. It is not a fallback
        # for not finding one: hashing every argument instead keys on arguments
        # the author never named, and deduplication silently never fires.
        value = _resolve_key_path(key, arguments, func.__name__)
        if hash_payload:
            return compute_payload_hash(value)
        return str(value)

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
            or callable extracting key from (*args, **kwargs). A name or path
            that does not resolve, or resolves to None, raises
            IdempotencyKeyError at call time, with or without hash_payload.
        hash_payload: If True, hashes with SHA-256 the value `key` resolves to,
            or every domain argument when no key is given.

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
        if inspect.isgeneratorfunction(target_func) or inspect.isasyncgenfunction(target_func):
            raise ConfigurationError(
                f"@idempotent cannot decorate {getattr(target_func, '__qualname__', target_func)!r}, "
                f"a generator: calling it only builds the generator, so the key would be "
                f"reset before its body ran and nothing it published would carry it."
            )
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
                sequence_token = idempotency_sequence_var.set([0])
                try:
                    return await target_func(*args, **kwargs)
                finally:
                    idempotency_sequence_var.reset(sequence_token)
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
            sequence_token = idempotency_sequence_var.set([0])
            try:
                return target_func(*args, **kwargs)
            finally:
                idempotency_sequence_var.reset(sequence_token)
                IdempotencyContext.reset(token)

        sync_wrapper._cliffracer_idempotent = True  # type: ignore[attr-defined]
        sync_wrapper._cliffracer_idempotency_key = effective_key  # type: ignore[attr-defined]
        sync_wrapper._cliffracer_hash_payload = effective_hash  # type: ignore[attr-defined]
        return sync_wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator
