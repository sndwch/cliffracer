"""Typed exceptions for cliffracer-cyanide."""

from __future__ import annotations


class CyanideError(Exception):
    """Base exception for all cyanide errors."""


class CyanideFaultError(CyanideError):
    """Raised when an injected fault error occurs."""


class CyanideDisabledError(CyanideError):
    """Raised when a cyanide fault mode is invoked but the extension is disabled."""


__all__ = [
    "CyanideDisabledError",
    "CyanideError",
    "CyanideFaultError",
]
