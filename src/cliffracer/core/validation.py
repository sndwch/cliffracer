"""
Input validation utilities for Cliffracer services.

Provides comprehensive validation for common parameters to prevent
security vulnerabilities and ensure data integrity.
"""

from typing import Any, TypeVar

from loguru import logger
from pydantic import BaseModel, Field, validator

T = TypeVar('T')


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


def validate_port(port: int) -> int:
    """
    Validate port number.

    Args:
        port: Port number to validate

    Returns:
        Validated port number

    Raises:
        ValidationError: If port is invalid
    """
    if not isinstance(port, int):
        raise ValidationError(f"Port must be an integer, got {type(port).__name__}")

    if port < NumericBounds.MIN_PORT or port > NumericBounds.MAX_PORT:
        raise ValidationError(
            f"Port must be between {NumericBounds.MIN_PORT} and {NumericBounds.MAX_PORT}, got {port}"
        )

    return port


def validate_timeout(timeout: int | float, min_ms: int = None, max_ms: int = None) -> float:
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


def validate_limit(limit: int, min_limit: int = None, max_limit: int = None) -> int:
    """
    Validate pagination limit.

    Args:
        limit: Number of items to return
        min_limit: Minimum allowed limit
        max_limit: Maximum allowed limit

    Returns:
        Validated limit

    Raises:
        ValidationError: If limit is invalid
    """
    if not isinstance(limit, int):
        raise ValidationError(f"Limit must be an integer, got {type(limit).__name__}")

    min_limit = min_limit or NumericBounds.MIN_LIMIT
    max_limit = max_limit or NumericBounds.MAX_LIMIT

    if limit < min_limit or limit > max_limit:
        raise ValidationError(
            f"Limit must be between {min_limit} and {max_limit}, got {limit}"
        )

    return limit


def validate_offset(offset: int) -> int:
    """
    Validate pagination offset.

    Args:
        offset: Number of items to skip

    Returns:
        Validated offset

    Raises:
        ValidationError: If offset is invalid
    """
    if not isinstance(offset, int):
        raise ValidationError(f"Offset must be an integer, got {type(offset).__name__}")

    if offset < 0:
        raise ValidationError(f"Offset must be non-negative, got {offset}")

    return offset


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
    min_length: int = None,
    max_length: int = None,
    field_name: str = "String"
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
        raise ValidationError(
            f"{field_name} must be at most {max_length} characters, got {length}"
        )

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
        field_name="Username"
    )

    # Additional username validation
    if not username.replace('_', '').replace('-', '').replace('.', '').isalnum():
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
        field_name="Password"
    )


def validate_sql_identifier(identifier: str, identifier_type: str = "identifier") -> str:
    """
    Validate SQL identifier (table/column name).

    Args:
        identifier: SQL identifier to validate
        identifier_type: Type of identifier for error messages

    Returns:
        Validated identifier

    Raises:
        ValidationError: If identifier is invalid
    """
    import re

    if not isinstance(identifier, str):
        raise ValidationError(f"SQL {identifier_type} must be a string")

    # Check length
    max_length = StringLimits.MAX_TABLE_NAME_LENGTH
    if len(identifier) > max_length:
        raise ValidationError(
            f"SQL {identifier_type} too long: {len(identifier)} > {max_length}"
        )

    # Check valid characters (letters, numbers, underscores)
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', identifier):
        raise ValidationError(
            f"SQL {identifier_type} '{identifier}' contains invalid characters. "
            "Must start with letter or underscore and contain only letters, numbers, and underscores."
        )

    # Check against SQL reserved words (basic list)
    reserved_words = {
        'select', 'insert', 'update', 'delete', 'drop', 'create',
        'alter', 'table', 'from', 'where', 'and', 'or', 'not',
        'union', 'join', 'order', 'group', 'having', 'limit'
    }

    if identifier.lower() in reserved_words:
        raise ValidationError(
            f"SQL {identifier_type} '{identifier}' is a reserved word"
        )

    return identifier


def validate_dict_not_empty(value: dict, field_name: str = "Dictionary") -> dict:
    """
    Validate that a dictionary is not empty.

    Args:
        value: Dictionary to validate
        field_name: Name of field for error messages

    Returns:
        Validated dictionary

    Raises:
        ValidationError: If dictionary is invalid or empty
    """
    if not isinstance(value, dict):
        raise ValidationError(f"{field_name} must be a dictionary, got {type(value).__name__}")

    if not value:
        raise ValidationError(f"{field_name} cannot be empty")

    return value


def validate_list_not_empty(value: list, field_name: str = "List") -> list:
    """
    Validate that a list is not empty.

    Args:
        value: List to validate
        field_name: Name of field for error messages

    Returns:
        Validated list

    Raises:
        ValidationError: If list is invalid or empty
    """
    if not isinstance(value, list):
        raise ValidationError(f"{field_name} must be a list, got {type(value).__name__}")

    if not value:
        raise ValidationError(f"{field_name} cannot be empty")

    return value


# Pydantic models for complex validation

class PaginationParams(BaseModel):
    """Validated pagination parameters"""

    limit: int = Field(
        default=NumericBounds.DEFAULT_LIMIT,
        ge=NumericBounds.MIN_LIMIT,
        le=NumericBounds.MAX_LIMIT,
        description="Number of items to return"
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of items to skip"
    )


class BatchConfig(BaseModel):
    """Validated batch processing configuration"""

    batch_size: int = Field(
        default=NumericBounds.DEFAULT_BATCH_SIZE,
        ge=NumericBounds.MIN_BATCH_SIZE,
        le=NumericBounds.MAX_BATCH_SIZE,
        description="Maximum items per batch"
    )
    batch_timeout_ms: int = Field(
        default=50,
        ge=1,
        le=60000,
        description="Timeout in milliseconds"
    )
    max_concurrent_batches: int = Field(
        default=NumericBounds.DEFAULT_CONCURRENT,
        ge=NumericBounds.MIN_CONCURRENT,
        le=NumericBounds.MAX_CONCURRENT,
        description="Maximum concurrent batch processes"
    )


class ServerConfig(BaseModel):
    """Validated server configuration"""

    host: str = Field(
        default="0.0.0.0",
        pattern=r'^(\d{1,3}\.){3}\d{1,3}$|^localhost$|^[a-zA-Z0-9.-]+$',
        description="Server host address"
    )
    port: int = Field(
        default=8000,
        ge=NumericBounds.MIN_PORT,
        le=NumericBounds.MAX_PORT,
        description="Server port"
    )

    @validator('port')
    def validate_port_not_privileged(cls, v):
        """Warn if using privileged port"""
        if v < 1024:
            logger.warning(f"Using privileged port {v} - may require elevated permissions")
        return v


def sanitize_for_logging(value: Any, max_length: int = 100) -> str:
    """
    Sanitize value for safe logging.

    Args:
        value: Value to sanitize
        max_length: Maximum length for string representation

    Returns:
        Safe string representation
    """
    str_value = str(value)

    # Truncate if too long
    if len(str_value) > max_length:
        str_value = str_value[:max_length] + "..."

    # Remove potential control characters
    str_value = ''.join(char for char in str_value if char.isprintable() or char.isspace())

    return str_value
