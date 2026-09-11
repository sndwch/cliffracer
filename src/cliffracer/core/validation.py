"""Runtime validation helpers for ports, timeouts, and payload encoding."""

import json
from typing import Any, TypeVar, cast

try:
    import msgpack
except ImportError:
    msgpack = None
import pydantic_core

T = TypeVar("T")


class ValidationError(ValueError):
    """Raised when validation fails"""

    pass


class NumericBounds:
    """Common numeric validation bounds"""

    # Port numbers
    MIN_PORT = 1
    MAX_PORT = 65535

    # Timeouts (milliseconds)
    MIN_TIMEOUT_MS = 1
    MAX_TIMEOUT_MS = 3600000  # 1 hour
    DEFAULT_TIMEOUT_MS = 30000  # 30 seconds

    # Limits
    MIN_LIMIT = 1
    MAX_LIMIT = 10000
    DEFAULT_LIMIT = 100

    # Batch sizes
    MIN_BATCH_SIZE = 1
    MAX_BATCH_SIZE = 10000
    DEFAULT_BATCH_SIZE = 100

    # Concurrent operations
    MIN_CONCURRENT = 1
    MAX_CONCURRENT = 1000
    DEFAULT_CONCURRENT = 10


class StringLimits:
    """Common string length limits"""

    # Identifiers
    MIN_IDENTIFIER_LENGTH = 1
    MAX_IDENTIFIER_LENGTH = 63  # PostgreSQL limit

    # User input
    MIN_USERNAME_LENGTH = 3
    MAX_USERNAME_LENGTH = 32

    MIN_PASSWORD_LENGTH = 8
    MAX_PASSWORD_LENGTH = 128

    # General strings
    MAX_STRING_LENGTH = 4096
    MAX_TEXT_LENGTH = 65536

    # SQL identifiers
    MAX_TABLE_NAME_LENGTH = 63
    MAX_COLUMN_NAME_LENGTH = 63


def validate_timeout(
    timeout: int | float, min_ms: int | None = None, max_ms: int | None = None
) -> float:
    """
    Validate timeout value.

    Args:
        timeout: Timeout in seconds or milliseconds
        min_ms: Minimum timeout in milliseconds
        max_ms: Maximum timeout in milliseconds

    Returns:
        Validated timeout in seconds

    Raises:
        ValidationError: If timeout is invalid
    """
    if not isinstance(timeout, int | float):
        raise ValidationError(f"Timeout must be numeric, got {type(timeout).__name__}")

    # Convert to milliseconds for validation
    timeout_ms = timeout * 1000 if timeout < 1000 else timeout

    min_ms = min_ms or NumericBounds.MIN_TIMEOUT_MS
    max_ms = max_ms or NumericBounds.MAX_TIMEOUT_MS

    if timeout_ms < min_ms or timeout_ms > max_ms:
        raise ValidationError(
            f"Timeout must be between {min_ms}ms and {max_ms}ms, got {timeout_ms}ms"
        )

    # Return in seconds
    return timeout_ms / 1000


def validate_batch_size(batch_size: int) -> int:
    """
    Validate batch size.

    Args:
        batch_size: Number of items per batch

    Returns:
        Validated batch size

    Raises:
        ValidationError: If batch size is invalid
    """
    if not isinstance(batch_size, int):
        raise ValidationError(f"Batch size must be an integer, got {type(batch_size).__name__}")

    if batch_size < NumericBounds.MIN_BATCH_SIZE or batch_size > NumericBounds.MAX_BATCH_SIZE:
        raise ValidationError(
            f"Batch size must be between {NumericBounds.MIN_BATCH_SIZE} and "
            f"{NumericBounds.MAX_BATCH_SIZE}, got {batch_size}"
        )

    return batch_size


def validate_string_length(
    value: str,
    min_length: int | None = None,
    max_length: int | None = None,
    field_name: str = "String",
) -> str:
    """
    Validate string length.

    Args:
        value: String to validate
        min_length: Minimum allowed length
        max_length: Maximum allowed length
        field_name: Name of field for error messages

    Returns:
        Validated string

    Raises:
        ValidationError: If string is invalid
    """
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string, got {type(value).__name__}")

    length = len(value)

    if min_length is not None and length < min_length:
        raise ValidationError(
            f"{field_name} must be at least {min_length} characters, got {length}"
        )

    if max_length is not None and length > max_length:
        raise ValidationError(f"{field_name} must be at most {max_length} characters, got {length}")

    return value


def validate_username(username: str) -> str:
    """
    Validate username.

    Args:
        username: Username to validate

    Returns:
        Validated username

    Raises:
        ValidationError: If username is invalid
    """
    username = validate_string_length(
        username,
        min_length=StringLimits.MIN_USERNAME_LENGTH,
        max_length=StringLimits.MAX_USERNAME_LENGTH,
        field_name="Username",
    )

    # Additional username validation
    if not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise ValidationError(
            "Username can only contain letters, numbers, underscores, hyphens, and dots"
        )

    return username.lower()  # Normalize to lowercase


def validate_password(password: str) -> str:
    """
    Validate password.

    Args:
        password: Password to validate

    Returns:
        Validated password

    Raises:
        ValidationError: If password is invalid
    """
    return validate_string_length(
        password,
        min_length=StringLimits.MIN_PASSWORD_LENGTH,
        max_length=StringLimits.MAX_PASSWORD_LENGTH,
        field_name="Password",
    )


# Serialization formats and Content-Type constants
CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_MSGPACK = "application/msgpack"
SUPPORTED_FORMATS = ("json", "msgpack")


def pack_msgpack(data: Any) -> bytes:
    """Pack data into msgpack bytes using pydantic_core.to_jsonable_python for complex types."""
    if msgpack is None:
        raise ImportError(
            "MessagePack serialization requires the 'msgpack' package. "
            "Install it with: pip install 'cliffracer[msgpack]'"
        )
    return cast(
        bytes, msgpack.packb(data, default=pydantic_core.to_jsonable_python, use_bin_type=True)
    )


def unpack_msgpack(raw: bytes) -> Any:
    """Unpack msgpack bytes into Python objects with strings decoded as str."""
    if msgpack is None:
        raise ImportError(
            "MessagePack serialization requires the 'msgpack' package. "
            "Install it with: pip install 'cliffracer[msgpack]'"
        )
    return msgpack.unpackb(raw, raw=False)


def serialize_payload(data: Any, format: str = "json") -> tuple[bytes, str]:
    """Serialize data into bytes and return along with its Content-Type."""
    fmt = (format or "json").lower()
    if fmt == "msgpack":
        return pack_msgpack(data), CONTENT_TYPE_MSGPACK
    elif fmt == "json":
        json_data = pydantic_core.to_jsonable_python(data)
        return json.dumps(json_data).encode("utf-8"), CONTENT_TYPE_JSON
    else:
        raise ValueError(f"Unsupported serialization format: {format}")


def deserialize_payload(
    raw: bytes,
    content_type: str | None = None,
    fallback_format: str = "json",
) -> Any:
    """Deserialize payload bytes based on Content-Type header with graceful fallback."""
    if not raw:
        return {}
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct == CONTENT_TYPE_MSGPACK or (not ct and fallback_format == "msgpack"):
        try:
            return unpack_msgpack(raw)
        except Exception as e:
            if not ct:
                # Fallback to json if untyped bytes fail msgpack
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    pass
            raise e
    else:
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            if not ct:
                # Fallback to msgpack if untyped bytes fail json
                if msgpack is not None:
                    try:
                        return unpack_msgpack(raw)
                    except Exception:
                        pass
            raise e
