"""A request's deadline: how long its caller still waits, carried from call to call.

A caller sends `Cliffracer-Timeout-Ms`, the whole milliseconds it will still wait for the reply
at the moment it sends. The budget is relative, never an instant, so two hosts need not agree on
the time: the receiver turns it into a deadline on its own event loop's clock when the request
reaches dispatch. The time the request spent on the wire is not visible to either side, so a
callee may work up to one transit longer than its caller waits, never less.

The RPC dispatcher bounds a handler by the earlier of that deadline and the service's own
`max_rpc_processing_time`, and holds the result in a context variable while the handler runs, so
a call the handler makes (`call_rpc`, `RpcProxy`, `ServiceClient`) waits at most what is left and
sends what is left, and a call with no time left is not sent.
"""

from __future__ import annotations

import asyncio
import contextvars
import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from .exceptions import RpcTimeoutError

#: The request header that carries the caller's remaining budget, in whole milliseconds.
TIMEOUT_HEADER = "Cliffracer-Timeout-Ms"

#: The largest budget read from the header: one day. A larger number is not a budget.
_CEILING_MS = 86_400_000


@dataclass(frozen=True)
class Deadline:
    """When a request's handler must be done, on the receiving loop's clock."""

    #: The loop time by which the handler must be done.
    at: float
    #: The seconds it was given when it arrived.
    budget: float
    #: Who set it: the caller's header, the service's `max_rpc_processing_time`, or a timer's
    #: `deadline=` for one of its firings.
    set_by: Literal["caller", "service", "timer"]

    def remaining(self) -> float:
        return self.at - asyncio.get_running_loop().time()


_CURRENT: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar(
    "cliffracer_request_deadline", default=None
)


def caller_budget(headers: Mapping[str, Any]) -> float | None:
    """The seconds the caller still waits, from `Cliffracer-Timeout-Ms` in any case, or None.

    Only a whole number of milliseconds from 1 to one day is a budget. Anything else (absent,
    empty, zero, negative, a fraction, text) is read as no budget at all: the header is optional,
    so a malformed one costs the request nothing it would have had without it.
    """
    wanted = TIMEOUT_HEADER.lower()
    for name, value in headers.items():
        if str(name).lower() != wanted:
            continue
        text = str(value).strip()
        if not (text.isascii() and text.isdigit()):
            return None
        millis = int(text)
        return millis / 1000 if 0 < millis <= _CEILING_MS else None
    return None


def on_arrival(headers: Mapping[str, Any], cap: float | None) -> Deadline | None:
    """The deadline of a request arriving now: the earlier of its caller's budget and `cap`."""
    budget = caller_budget(headers)
    candidates: list[tuple[float, Literal["caller", "service"]]] = []
    if budget is not None:
        candidates.append((budget, "caller"))
    if cap is not None:
        candidates.append((cap, "service"))
    if not candidates:
        return None
    seconds, set_by = min(candidates, key=lambda candidate: candidate[0])
    return Deadline(asyncio.get_running_loop().time() + seconds, seconds, set_by)


def current() -> Deadline | None:
    """The deadline of the request whose handler is running in this context, if it has one."""
    return _CURRENT.get()


@contextmanager
def scoped(deadline: Deadline | None) -> Iterator[None]:
    """Make `deadline` the current one for the duration, and restore what was there after."""
    token = _CURRENT.set(deadline)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def outbound_timeout(own: float, call: str) -> float:
    """The seconds `call`, made now, waits: its own timeout, or what its request has left if less.

    Raises `RpcTimeoutError` when the request it is part of has no time left: the call is not to
    be sent.
    """
    deadline = current()
    if deadline is None:
        return own
    left = min(own, deadline.remaining())
    if left <= 0:
        raise RpcTimeoutError(
            f"{call} was not sent: the request this call is part of has no time left"
        )
    return left


def header_value(seconds: float) -> str:
    """`seconds` as the header carries it: whole milliseconds, at least one."""
    return str(max(1, math.floor(seconds * 1000)))


def refuse_a_duration(name: str, seconds: Any) -> None:
    """Refuse, naming `name`, a wait that is not a positive, finite number of seconds: zero or less
    fails every call for a reason that is not the real one, and NaN cannot be sent as a budget."""
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int | float)
        or not math.isfinite(seconds)
        or seconds <= 0
    ):
        raise ValueError(f"{name} must be a positive, finite number of seconds, not {seconds!r}")
