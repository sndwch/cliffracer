"""Exceptions for cliffracer-kv."""

from __future__ import annotations


class KvError(Exception):
    """Base exception for all cliffracer-kv errors."""


class JetStreamUnavailableError(KvError):
    """Raised when JetStream is required by KvExtension but is not available."""


class BucketConfigError(KvError):
    """Raised when bucket or object store configuration is invalid."""
