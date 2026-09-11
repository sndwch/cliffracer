"""Configuration models for cliffracer-kv buckets and object stores."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .errors import BucketConfigError


def normalize_ttl_seconds(ttl: Any) -> float | None:
    """Convert float, int, or timedelta to float seconds.

    Returns None if ttl is None.
    """
    if ttl is None:
        return None
    if isinstance(ttl, timedelta):
        return ttl.total_seconds()
    if isinstance(ttl, int | float):
        return float(ttl)
    try:
        return float(ttl)
    except (ValueError, TypeError) as err:
        raise BucketConfigError(
            f"Invalid TTL value {ttl!r}: must be float, int, or timedelta"
        ) from err


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
    storage: str | None = None

    @classmethod
    def from_value(
        cls,
        value: str | BucketConfig | dict[str, Any],
        default_ttl: float | int | timedelta | None = None,
    ) -> BucketConfig:
        """Create a BucketConfig from a string name, existing BucketConfig, or dictionary."""
        if isinstance(value, cls):
            if value.ttl is None and default_ttl is not None:
                return cls(
                    name=value.name,
                    ttl=default_ttl,
                    description=value.description,
                    history=value.history,
                    max_bytes=value.max_bytes,
                    max_value_size=value.max_value_size,
                    replicas=value.replicas,
                    storage=value.storage,
                )
            return value
        if isinstance(value, str):
            return cls(name=value, ttl=default_ttl)
        if isinstance(value, dict):
            name = value.get("name") or value.get("bucket")
            if not name or not isinstance(name, str):
                raise BucketConfigError(
                    f"Bucket config dictionary must contain a valid string 'name' or 'bucket': {value!r}"
                )
            ttl = value.get("ttl", default_ttl)
            return cls(
                name=name,
                ttl=ttl,
                description=value.get("description"),
                history=value.get("history", 1),
                max_bytes=value.get("max_bytes"),
                max_value_size=value.get("max_value_size"),
                replicas=value.get("replicas", 1),
                storage=value.get("storage"),
            )
        raise BucketConfigError(
            f"Cannot create BucketConfig from {type(value).__name__}: {value!r}"
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

    @classmethod
    def from_value(
        cls,
        value: str | ObjectStoreConfig | dict[str, Any],
        default_ttl: float | int | timedelta | None = None,
    ) -> ObjectStoreConfig:
        """Create an ObjectStoreConfig from a string name, existing config, or dictionary."""
        if isinstance(value, cls):
            if value.ttl is None and default_ttl is not None:
                return cls(
                    name=value.name,
                    ttl=default_ttl,
                    description=value.description,
                    max_bytes=value.max_bytes,
                    replicas=value.replicas,
                    storage=value.storage,
                )
            return value
        if isinstance(value, str):
            return cls(name=value, ttl=default_ttl)
        if isinstance(value, dict):
            name = value.get("name") or value.get("bucket")
            if not name or not isinstance(name, str):
                raise BucketConfigError(
                    f"ObjectStore config dictionary must contain a valid string 'name' or 'bucket': {value!r}"
                )
            ttl = value.get("ttl", default_ttl)
            return cls(
                name=name,
                ttl=ttl,
                description=value.get("description"),
                max_bytes=value.get("max_bytes"),
                replicas=value.get("replicas", 1),
                storage=value.get("storage"),
            )
        raise BucketConfigError(
            f"Cannot create ObjectStoreConfig from {type(value).__name__}: {value!r}"
        )
