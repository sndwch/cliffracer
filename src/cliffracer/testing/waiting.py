"""Cooperative waits with useful failure messages for asynchronous tests."""

from __future__ import annotations

import inspect
import math
from asyncio import sleep
from collections.abc import Callable
from time import monotonic

_POLL_INTERVAL_SECONDS = 0.01


async def wait_until(condition: Callable[[], object], *, within: float, reason: str) -> None:
    """Wait until a synchronous observation is true or fail with ``reason``.

    A fixed sleep before an assertion bets that the producer will run before
    the sleep ends, which makes the assertion fail for the wrong reason on a
    slow host. Wait for a positive completion or readiness observation instead.

    This helper cannot prove that something never happened. A condition such
    as ``lambda: not events`` succeeds before the producer gets a chance to run;
    wait for the producer's completion barrier, then assert that its prohibited
    effect is absent.
    """
    if isinstance(within, bool) or not isinstance(within, int | float):
        raise TypeError("within must be a number of seconds")
    if not math.isfinite(within) or within <= 0:
        raise ValueError("within must be a finite number greater than zero")
    if not isinstance(reason, str):
        raise TypeError("reason must be a string")
    if not reason.strip():
        raise ValueError("reason must describe what the test was waiting for")

    started = monotonic()
    deadline = started + within
    while True:
        observed = condition()
        if inspect.isawaitable(observed):
            if inspect.iscoroutine(observed):
                observed.close()
            raise TypeError("wait_until condition must be synchronous")
        ready = bool(observed)
        now = monotonic()
        if ready and now <= deadline:
            return
        if now >= deadline:
            elapsed = now - started
            raise AssertionError(f"Timed out after {elapsed:.3f}s (budget {within:g}s): {reason}")
        await sleep(min(_POLL_INTERVAL_SECONDS, deadline - now))


__all__ = ["wait_until"]
