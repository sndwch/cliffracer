"""Where an existing bucket's configuration disagrees with the one a service declares.

A bucket's stream is configured once, when it is created. Opening an existing bucket applies
nothing, so a declaration that changes after the first deploy is accepted and ignored. This
module reads the stream's configuration back and names each option that differs, so that the
extension can say so.

Only an option the declaration sets is compared: a default (`history=1`, `replicas=1`, no `ttl`)
is the absence of a request, and a bucket someone else created with other settings is not drift
from a service that never asked for any.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from .config import BucketConfig, ObjectStoreConfig, normalize_ttl_seconds

# (option on the declaration, attribute on the stream's configuration)
_BUCKET_OPTIONS = (
    ("ttl", "max_age"),
    ("description", "description"),
    ("history", "max_msgs_per_subject"),
    ("max_bytes", "max_bytes"),
    ("max_value_size", "max_msg_size"),
    ("replicas", "num_replicas"),
    ("storage", "storage"),
    ("direct", "allow_direct"),
    ("republish", "republish"),
    ("placement", "placement"),
)
_OBJECT_STORE_OPTIONS = (
    ("ttl", "max_age"),
    ("description", "description"),
    ("max_bytes", "max_bytes"),
    ("replicas", "num_replicas"),
    ("storage", "storage"),
)

_NUMERIC = frozenset(
    {"max_age", "max_msgs_per_subject", "max_bytes", "max_msg_size", "num_replicas"}
)

# What a declaration holds when it asks for nothing, for the options that default to a value.
_UNASKED = {"history": 1, "replicas": 1}


def _storage(value: Any) -> str | None:
    """`StorageType.FILE` and "file" are the same request; anything else is not a storage."""
    inner = value.value if isinstance(value, Enum) else value
    return inner if isinstance(inner, str) else None


def _republish(value: Any) -> dict[str, Any] | None:
    """A republish rule as the three things it says; `None` for no rule or a stand-in."""
    if value is None:
        return None
    src, dest = getattr(value, "src", None), getattr(value, "dest", None)
    if not isinstance(src, str) or not isinstance(dest, str):
        return None
    return {"src": src, "dest": dest, "headers_only": bool(getattr(value, "headers_only", False))}


def _placement(value: Any) -> dict[str, Any] | None:
    """A placement as its cluster and tags (order does not matter); `None` for none or a stand-in."""
    if value is None:
        return None
    cluster, tags = getattr(value, "cluster", None), getattr(value, "tags", None)
    if cluster is not None and not isinstance(cluster, str):
        return None
    if tags is not None and not isinstance(tags, list | tuple):
        return None
    return {"cluster": cluster, "tags": sorted(tags or [])}


def _declared(config: BucketConfig | ObjectStoreConfig, option: str) -> Any:
    value = getattr(config, option, None)
    if value is None or _UNASKED.get(option) == value:
        return None
    if option == "ttl":
        return normalize_ttl_seconds(value)
    if option == "storage":
        return _storage(value)
    if option == "republish":
        return _republish(value)
    if option == "placement":
        return _placement(value)
    return value


_UNREADABLE = object()


def _actual(stream: Any, attribute: str) -> Any:
    """The stream's value for an attribute, or `_UNREADABLE` when it is not of the kind a broker
    returns, so a stand-in for the stream is never compared as if it were one."""
    value = getattr(stream, attribute, None)
    if attribute in _NUMERIC:
        if value is None and attribute == "max_age":
            # The broker's "no limit" is 0 for an age, and a declared ttl of 0 asks for the same.
            return 0.0
        if not isinstance(value, int | float) or isinstance(value, bool):
            return _UNREADABLE
        return value
    if attribute == "storage":
        text = _storage(value)
        return text if isinstance(text, str) else _UNREADABLE
    if attribute == "description":
        return value if value is None or isinstance(value, str) else _UNREADABLE
    if attribute == "allow_direct":
        return value if isinstance(value, bool) else _UNREADABLE
    if attribute in ("republish", "placement"):
        # No rule and no placement are both a broker answer; anything not shaped like the native
        # type is a stand-in and is not compared.
        if value is None:
            return None
        shaped = _republish(value) if attribute == "republish" else _placement(value)
        return shaped if shaped is not None else _UNREADABLE
    return value


def _differences(
    config: BucketConfig | ObjectStoreConfig,
    stream: Any,
    options: tuple[tuple[str, str], ...],
) -> list[tuple[str, Any, Any]]:
    found: list[tuple[str, Any, Any]] = []
    for option, attribute in options:
        asked = _declared(config, option)
        if asked is None:
            continue
        have = _actual(stream, attribute)
        if have is not _UNREADABLE and asked != have:
            found.append((option, asked, have))
    return found


def declares_options(config: BucketConfig | ObjectStoreConfig) -> bool:
    """Whether the declaration sets any option worth comparing: a bare name asks for nothing."""
    return any(_declared(config, option) is not None for option, _ in _BUCKET_OPTIONS)


def bucket_drift(config: BucketConfig, stream: Any) -> list[tuple[str, Any, Any]]:
    """`(option, declared, on the broker)` for each option the bucket declaration sets that differs."""
    return _differences(config, stream, _BUCKET_OPTIONS)


def object_store_drift(config: ObjectStoreConfig, stream: Any) -> list[tuple[str, Any, Any]]:
    """The same for an object store's declaration."""
    return _differences(config, stream, _OBJECT_STORE_OPTIONS)


def describe(kind: str, name: str, drift: list[tuple[str, Any, Any]]) -> str:
    """The warning for an existing bucket or store whose configuration differs."""
    fields = "; ".join(
        f"{option}: declared {asked!r}, on the broker {have!r}" for option, asked, have in drift
    )
    return (
        f"{kind} {name!r} already exists and its configuration differs from this service's "
        f"declaration ({fields}). An existing {kind.lower()} keeps its configuration: the "
        f"declaration is not applied. Change it on the broker, or align the declaration."
    )
