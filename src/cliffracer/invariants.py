"""Architectural invariant enforcement and exemptions."""

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T", bound=type)


def override_length_check(reason: str) -> Callable[[T], T]:
    """Exempt a class from the 500 AST statement complexity ceiling.

    Requires an explicit, non-empty reason string explaining why the class
    complexity cannot be decomposed into smaller collaborator classes.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("override_length_check requires a non-empty reason string")

    def decorator(cls: T) -> T:
        setattr(cls, "__override_length_check_reason__", reason.strip())  # noqa: B010
        return cls

    return decorator
