"""Configuration models for cliffracer-kv buckets and object stores."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from datetime import timedelta
from math import isfinite
from typing import Any, cast

from nats.js.api import Placement, RePublish, StorageType

from .errors import BucketConfigError

#: JetStream retains a message for at least this long; a smaller non-zero maximum
#: age is refused by the server when the bucket is created.
MIN_TTL_SECONDS = 0.1

#: The most revisions of one key a bucket can keep.
MAX_HISTORY = 64

_STORAGE_TYPES = ("file", "memory")

#: What JetStream accepts as the name of a bucket or an object store. A `.` is not among them: it
#: separates the tokens of a subject, and belongs in a key.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def normalize_ttl_seconds(ttl: Any) -> float | None:
    """Convert float, int, or timedelta to float seconds.

    Returns None if ttl is None. Zero means no expiry. Anything else must be a
    finite, non-negative duration of at least 100 ms, the shortest JetStream
    accepts; a bool or a string is refused rather than read as a number.
    """
    if ttl is None:
        return None
    if isinstance(ttl, bool) or not isinstance(ttl, int | float | timedelta):
        raise BucketConfigError(f"Invalid TTL value {ttl!r}: must be float, int, or timedelta")
    seconds = ttl.total_seconds() if isinstance(ttl, timedelta) else float(ttl)
    if not isfinite(seconds) or seconds < 0:
        raise BucketConfigError(
            f"Invalid TTL value {ttl!r}: must be a finite number of seconds, not negative"
        )
    if 0 < seconds < MIN_TTL_SECONDS:
        raise BucketConfigError(
            f"Invalid TTL value {ttl!r}: JetStream keeps a message for at least "
            f"{MIN_TTL_SECONDS} seconds (use 0 for no expiry)"
        )
    return seconds


def _checked_ttl(owner: str, ttl: Any) -> None:
    try:
        normalize_ttl_seconds(ttl)
    except BucketConfigError as exc:
        raise BucketConfigError(f"{owner}: {exc}") from exc


def _checked_count(
    owner: str, field: str, value: Any, *, low: int, high: int | None = None
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise BucketConfigError(
            f"{owner}: {field} must be an integer of at least {low}, got {value!r}"
        )
    if high is not None and value > high:
        raise BucketConfigError(
            f"{owner}: {field} must be an integer of at most {high}, got {value!r}"
        )


def _checked_name(kind: str, name: Any) -> None:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise BucketConfigError(
            f"{kind} {name!r}: the name may hold only letters, digits, '_' and '-' "
            "(a '.' or a space is not allowed; a '.' belongs in a key)"
        )


def _checked_optional_count(owner: str, field: str, value: Any) -> None:
    """A size: unset, `-1` for no limit, or a whole number of bytes of at least 1."""
    if value is not None and value != -1:
        _checked_count(owner, field, value, low=1)


def _checked_instance(owner: str, field: str, value: Any, kind: type | tuple[type, ...]) -> None:
    if value is not None and not isinstance(value, kind):
        names = kind.__name__ if isinstance(kind, type) else " or ".join(k.__name__ for k in kind)
        raise BucketConfigError(f"{owner}: {field} must be a {names} or unset, got {value!r}")


def _checked_storage(owner: str, storage: Any) -> None:
    if storage is None or isinstance(storage, StorageType):
        return
    if isinstance(storage, str) and storage in _STORAGE_TYPES:
        return
    raise BucketConfigError(
        f"{owner}: storage must be one of {', '.join(map(repr, _STORAGE_TYPES))} "
        f"(lower case) or a StorageType, got {storage!r}"
    )


def _unknown_keys(owner: str, value: dict[str, Any], known: set[str]) -> None:
    unknown = sorted(set(value) - known)
    if unknown:
        raise BucketConfigError(
            f"{owner}: unknown option(s) {unknown}; the options are {sorted(known)}"
        )


def _ttl_or_default(value: dict[str, Any], default_ttl: Any) -> Any:
    """`ttl` from a dictionary, where a missing key and an explicit `None` both mean unset."""
    ttl = value.get("ttl")
    return default_ttl if ttl is None else ttl


def normalize_message_ttl(ttl: float | int | timedelta) -> float:
    """Return whole positive seconds without silently truncating a duration."""
    if isinstance(ttl, bool) or not isinstance(ttl, int | float | timedelta):
        raise BucketConfigError("Message TTL must be a whole positive number of seconds")
    seconds = ttl.total_seconds() if isinstance(ttl, timedelta) else float(ttl)
    if not isfinite(seconds) or seconds < 1 or not seconds.is_integer():
        raise BucketConfigError("Message TTL must be a whole positive number of seconds")
    return seconds


def declared_name(value: Any, config_type: Any) -> Any:
    """The name a declaration carries: the string itself, an instance's `name`, or a
    dictionary's `name` or `bucket`. `None` when a dictionary names neither."""
    if isinstance(value, str):
        return value
    if isinstance(value, config_type):
        return value.name
    if isinstance(value, dict):
        return value.get("name") or value.get("bucket")
    raise BucketConfigError(
        f"Cannot declare a {config_type.__name__} from {type(value).__name__}: {value!r}; "
        "a declaration is a name, a configuration object or a dictionary of its options"
    )


def _from_value(
    cls: Any,
    value: Any,
    default_ttl: Any,
    *,
    noun: str,
    owner: str,
    convert: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
) -> Any:
    """Build `cls` from a name, an instance or a dictionary of its options.

    `noun` and `owner` only spell the messages. `convert` maps an option to a function that
    turns the dictionary's value for it into what the class holds.
    """
    if isinstance(value, cls):
        if value.ttl is None and default_ttl is not None:
            return replace(value, ttl=default_ttl)
        return value
    if isinstance(value, str):
        return cls(name=value, ttl=default_ttl)
    if isinstance(value, dict):
        name = value.get("name") or value.get("bucket")
        if not name or not isinstance(name, str):
            raise BucketConfigError(
                f"{noun} config dictionary must contain a valid string 'name' or 'bucket': {value!r}"
            )
        _unknown_keys(f"{owner} {name!r}", value, {f.name for f in fields(cls)} | {"bucket"})
        options = {
            f.name: value[f.name]
            for f in fields(cls)
            if f.name not in ("name", "ttl") and f.name in value
        }
        for option, convert_one in (convert or {}).items():
            if option in options:
                try:
                    options[option] = convert_one(options[option])
                except (TypeError, ValueError) as exc:
                    raise BucketConfigError(
                        f"{owner} {name!r}: {option} {options[option]!r} cannot be read: {exc}"
                    ) from exc
        return cls(name=name, ttl=_ttl_or_default(value, default_ttl), **options)
    raise BucketConfigError(f"Cannot create {cls.__name__} from {type(value).__name__}: {value!r}")


@dataclass
class BucketConfig:
    """Configuration for a NATS JetStream Key-Value bucket."""

    name: str
    ttl: float | int | timedelta | None = None
    description: str | None = None
    history: int = 1
    max_bytes: int | None = None
    max_value_size: int | None = None
    replicas: int = 1
    storage: StorageType | str | None = None
    limit_marker_ttl: float | int | timedelta | None = None
    placement: Placement | None = None
    republish: RePublish | None = None
    direct: bool | None = None

    def __post_init__(self) -> None:
        owner = f"Bucket {self.name!r}"
        _checked_name("Bucket", self.name)
        _checked_ttl(owner, self.ttl)
        _checked_count(owner, "history", self.history, low=1, high=MAX_HISTORY)
        _checked_count(owner, "replicas", self.replicas, low=1)
        _checked_storage(owner, self.storage)
        _checked_instance(owner, "description", self.description, str)
        _checked_optional_count(owner, "max_bytes", self.max_bytes)
        _checked_optional_count(owner, "max_value_size", self.max_value_size)
        _checked_instance(owner, "direct", self.direct, bool)
        _checked_instance(owner, "placement", self.placement, Placement)
        _checked_instance(owner, "republish", self.republish, RePublish)
        if self.limit_marker_ttl is not None:
            try:
                normalize_message_ttl(self.limit_marker_ttl)
            except BucketConfigError as exc:
                raise BucketConfigError(f"{owner}: limit_marker_ttl: {exc}") from exc

    @classmethod
    def from_value(
        cls,
        value: str | BucketConfig | dict[str, Any],
        default_ttl: float | int | timedelta | None = None,
    ) -> BucketConfig:
        """Create a BucketConfig from a string name, existing BucketConfig, or dictionary."""
        return cast(
            "BucketConfig",
            _from_value(
                cls,
                value,
                default_ttl,
                noun="Bucket",
                owner="Bucket",
                convert={
                    "placement": lambda v: Placement(**v) if isinstance(v, dict) else v,
                    "republish": lambda v: RePublish(**v) if isinstance(v, dict) else v,
                },
            ),
        )


@dataclass
class ObjectStoreConfig:
    """Configuration for a NATS JetStream Object Store."""

    name: str
    ttl: float | int | timedelta | None = None
    description: str | None = None
    max_bytes: int | None = None
    replicas: int = 1
    storage: str | None = None

    def __post_init__(self) -> None:
        owner = f"Object store {self.name!r}"
        _checked_name("Object store", self.name)
        _checked_ttl(owner, self.ttl)
        _checked_count(owner, "replicas", self.replicas, low=1)
        _checked_storage(owner, self.storage)
        _checked_instance(owner, "description", self.description, str)
        _checked_optional_count(owner, "max_bytes", self.max_bytes)

    @classmethod
    def from_value(
        cls,
        value: str | ObjectStoreConfig | dict[str, Any],
        default_ttl: float | int | timedelta | None = None,
    ) -> ObjectStoreConfig:
        """Create an ObjectStoreConfig from a string name, existing config, or dictionary."""
        return cast(
            "ObjectStoreConfig",
            _from_value(cls, value, default_ttl, noun="ObjectStore", owner="Object store"),
        )
