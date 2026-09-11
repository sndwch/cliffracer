"""Serialization and deserialization helpers for cliffracer-kv."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any, cast

from pydantic import BaseModel


def serialize_value(value: Any) -> bytes:
    """Serialize a Python object to bytes for NATS KV storage.

    Supported formats:
    - Pydantic BaseModel -> model_dump_json() utf-8 bytes
    - bytes, bytearray -> raw bytes
    - str -> utf-8 bytes
    - dict, list, int, float, bool, None -> json.dumps() utf-8 bytes
    - Other types -> JSON dump fallback, else str() utf-8 bytes
    """
    if isinstance(value, BaseModel):
        return value.model_dump_json().encode("utf-8")
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, dict | list | int | float | bool) or value is None:
        return json.dumps(value).encode("utf-8")

    try:
        return json.dumps(value).encode("utf-8")
    except (TypeError, ValueError):
        return str(value).encode("utf-8")


def deserialize_value[T](
    data: bytes | None,
    as_type: type[T] | None = None,
    default: Any = None,
) -> T | Any:
    """Deserialize raw bytes from NATS KV storage into a target type or inferred structure.

    - If data is None, returns default.
    - If as_type is a Pydantic BaseModel subclass, uses model_validate_json.
    - If as_type is bytes, returns data directly.
    - If as_type is str, decodes data as utf-8.
    - If as_type in (dict, list), parses as JSON.
    - If as_type is callable, attempts JSON parse first and passes to as_type.
    - If as_type is None, parses JSON if valid, else returns decoded utf-8 string,
      or raw bytes if non-decodable.
    """
    if data is None:
        return default

    if as_type is not None:
        if inspect.isclass(as_type) and issubclass(as_type, BaseModel):
            return as_type.model_validate_json(data)
        if as_type is bytes:
            return data
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            if as_type is str:
                return data.decode("utf-8", errors="replace")
            raise
        if as_type is str:
            return text
        if as_type in (dict, list):
            return json.loads(text)
        if callable(as_type):
            as_fn = cast(Callable[..., Any], as_type)
            try:
                parsed = json.loads(text)
                return as_fn(parsed)
            except Exception:
                return as_fn(text)

    # Inferred deserialization
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
