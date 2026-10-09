"""A concurrency limit per handler, declared on its decorator as `max_concurrency=`.

One limit per method: a method reachable on its `rpc` and `async` subjects, or listening on several
patterns, holds one semaphore across all of them, because what the limit protects is the method's
work. A request takes its handler's permit first and the service's permit second
(`max_rpc_concurrency`, `max_async_rpc_concurrency` or `max_event_concurrency`), so a request
waiting at a full handler holds nothing another method needs. Both waits are bounded by the one
request deadline, and a handler permit taken without the service permit that follows is returned.

A request (`@rpc`, `@async_rpc`) may also be bounded in how many wait at a full method,
`max_queued=`: it is counted in the delivery callback, as `max_rpc_in_flight` admission is, and a
request that finds `limit + max_queued` of its method's requests already admitted is answered
`busy` without taking an admission slot. Unset, and with `max_rpc_in_flight` set, it is half that
bound (at least 1), so one method's queue cannot hold every slot and starve the others. Events
take no admission slot, so the listener decorators do not take it.

The counts are the service's own, under `/health`'s `handler_limits`: an extension's hooks run
after both permits are held, so they cannot see a wait or a request that was never started.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..deadline import Deadline
from .rpc_limits import answer_busy, permit_within


@dataclass
class HandlerLimit:
    """One method's limit, its semaphore, and what is happening at it."""

    name: str
    limit: int
    #: The most requests that may wait at the full method, or None for no bound.
    max_queued: int | None = None
    sem: asyncio.Semaphore = field(init=False, repr=False)
    #: Holding this handler's permit.
    in_flight: int = 0
    #: Waiting for this handler's permit, or holding it and waiting for the service's.
    waiting: int = 0
    #: Not started: turned away over `max_queued`, or waited and the deadline passed or the
    #: service was stopping.
    refused: int = 0
    #: Requests admitted for this method and not yet finished, running or waiting.
    admitted: int = 0

    def __post_init__(self) -> None:
        self.sem = asyncio.Semaphore(self.limit)

    def details(self) -> dict[str, int | None]:
        return {
            "limit": self.limit,
            "max_queued": self.max_queued,
            "in_flight": self.in_flight,
            "waiting": self.waiting,
            "refused": self.refused,
        }


class HandlerLimits:
    """The limits of one service's handlers, each made the first time it is asked for."""

    def __init__(self, default_max_queued: Callable[[], int | None] = lambda: None) -> None:
        self._by_name: dict[str, HandlerLimit] = {}
        self._default_max_queued = default_max_queued

    def of(self, handler: Any) -> HandlerLimit | None:
        """`handler`'s limit, or None when its decorator declared none."""
        limit = getattr(handler, "_cliffracer_max_concurrency", None)
        if limit is None:
            return None
        name = getattr(handler, "__name__", repr(handler))
        held = self._by_name.get(name)
        if held is None:
            queued = getattr(handler, "_cliffracer_max_queued", None)
            if queued is None:
                queued = self._default_max_queued()
            held = self._by_name[name] = HandlerLimit(name, limit, queued)
        return held

    def details(self) -> dict[str, dict[str, int | None]]:
        """Each limit made so far, by method name."""
        return {name: held.details() for name, held in sorted(self._by_name.items())}


async def take(
    held: HandlerLimit | None, service: asyncio.Semaphore | None, deadline: Deadline | None
) -> bool:
    """Take the handler's permit, then the service's, both by `deadline`; False when it passed.

    A handler permit taken and not followed by the service's is returned before this returns.
    """
    if held is not None:
        held.waiting += 1
        try:
            if not await permit_within(held.sem, deadline):
                held.refused += 1
                return False
            try:
                if service is not None and not await permit_within(service, deadline):
                    held.refused += 1
                    held.sem.release()
                    return False
            except BaseException:
                held.sem.release()
                raise
        finally:
            held.waiting -= 1
        held.in_flight += 1
        return True
    return service is None or await permit_within(service, deadline)


def release(held: HandlerLimit | None, service: asyncio.Semaphore | None) -> None:
    """Return the permits `take` gave."""
    if service is not None:
        service.release()
    if held is not None:
        held.in_flight -= 1
        held.sem.release()


def not_started(held: HandlerLimit | None) -> None:
    """Count a request that held its permits and was not started (the service is stopping)."""
    if held is not None:
        held.refused += 1


def would_wait(held: HandlerLimit | None, service: asyncio.Semaphore | None) -> bool:
    """Whether either permit is all taken, so `take` would wait."""
    return (held is not None and held.sem.locked()) or (service is not None and service.locked())


def default_max_queued(in_flight_bound: int | None) -> int | None:
    """A limited method's `max_queued` when it declares none: half the admission bound, at least
    1, or no bound when the service admits without one."""
    return None if in_flight_bound is None else max(1, in_flight_bound // 2)


def refuse_when_queue_full(held: HandlerLimit | None) -> str | None:
    """Why a request for `held`'s method is not admitted, counted refused; None to admit it."""
    if held is None or held.max_queued is None:
        return None
    if held.admitted < held.limit + held.max_queued:
        return None
    held.refused += 1
    return (
        f"{held.name} not admitted: {held.limit} running and {held.max_queued} waiting, the "
        f"most this method takes"
    )


async def answer_queue_full(msg: Any, held: Any, text: str, logger: Any) -> None:
    """Log `text` and answer `busy`, when the request waits for a reply, for a request over its
    method's queue: `limit` is the most the method admits (its limit plus `max_queued`), and
    `in_flight` how many it holds."""
    logger.warning(text)
    if getattr(msg, "reply", True):
        await answer_busy(
            msg, text, logger, limit=held.limit + held.max_queued, in_flight=held.admitted
        )


def admit_to_method(held: HandlerLimit | None, task: Any) -> None:
    """Count `task` as admitted for `held`'s method until it is done, however it ends."""
    if held is None:
        return
    method = held
    method.admitted += 1

    def finished(_task: Any) -> None:
        method.admitted -= 1

    task.add_done_callback(finished)


def health_details(limits: HandlerLimits, registry: Any) -> dict[str, Any]:
    """`/health`'s `handler_limits`: every registered handler that declares a limit, by name.

    Absent when none declares one, so a service without limits reports what it always did.
    """
    for handler in [*registry.rpc_handlers.values(), *registry.event_handlers.values()]:
        limits.of(handler)
    details = limits.details()
    return {"handler_limits": details} if details else {}
