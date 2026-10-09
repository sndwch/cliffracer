"""One decision about whether an exception's own words may leave the process.

The RPC path has always gated this: a handler's exception reaches the wire only
when `expose_internal_errors` is set, and otherwise the caller gets a fixed
sentence. The health endpoint had no such gate on any of its five error paths,
and it is reachable with no authentication at all -- so a probe that failed
with a DSN in its message published the DSN, credentials included.

The gate lives here rather than at each site because five sites deciding
separately is five chances to decide differently, and the one that gets it
wrong is unauthenticated. The count is five and not four because a sweep for
the key `"error"` missed the site that writes `details_error` -- which is the
argument for one gate rather than a careful search, stated by the thing that
happened while writing this. A caller reading `expose_internal_errors` should get
one answer from the whole process.

WHAT IS NOT GATED, deliberately: our own text. "timed out after 2.0s" names a
bound this process chose and describes nothing a caller did not already know
from asking. The `ok` flag, the status and the list of unhealthy dependency
NAMES are likewise unchanged -- an operator has to be able to see WHICH
dependency is down without being told what it said. Only the exception's own
words are withheld.
"""

from __future__ import annotations

from typing import Any

#: What a surface says instead of an exception's text when the policy hides it.
INTERNAL_ERROR = "internal error"


def may_expose(config: Any) -> bool:
    """Whether this configuration permits exception text on a remote surface.

    Read with `getattr` because the health endpoint serves objects that are not
    always a `ServiceConfig` -- a test double, or a service constructed before
    its config is attached -- and the safe answer for "no config" is no.

    `is True` rather than truthiness, so this fails CLOSED. `getattr` on a
    `Mock` returns a child `Mock`, which is truthy, so a truthiness test
    published exception text for any service whose config was a test double --
    the wrong direction for a gate whose whole job is withholding. The real
    field is a pydantic `bool`, so nothing legitimate is refused by requiring
    the value itself.
    """
    return getattr(config, "expose_internal_errors", False) is True


def exception_text(exc: BaseException, config: Any, *, generic: str = INTERNAL_ERROR) -> str:
    """The text a remote surface may carry for `exc`.

    `generic` lets a site say something useful about WHERE the failure was
    without saying what it was, which is the distinction that makes a hidden
    error still actionable: "dependency checks unavailable" tells an operator
    which subsystem to look at, and the log still holds the exception.
    """
    if may_expose(config):
        return f"{type(exc).__name__}: {exc}"
    return generic


__all__ = ["INTERNAL_ERROR", "exception_text", "may_expose"]


#: Set on an exception whose text the framework wrote itself, a sentence about a limit or a missing
#: package that holds nothing the application put there, so a surface that withholds an exception's
#: text may still carry it.
OWN_TEXT_ATTR = "_cliffracer_own_text"


def own_text(error: BaseException) -> BaseException:
    """Mark *error* as one whose text is the framework's own, and return it."""
    setattr(error, OWN_TEXT_ATTR, True)
    return error


def has_own_text(error: BaseException) -> bool:
    """Whether the framework wrote *error*'s text itself (see `own_text`)."""
    return getattr(error, OWN_TEXT_ATTR, False) is True
