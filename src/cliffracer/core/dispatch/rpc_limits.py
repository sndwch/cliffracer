"""What bounds an RPC request: its deadline, the permit it waits for, and the admission count.

The RPC dispatcher's delivery callback fixes a request's deadline, admits it against
`max_rpc_in_flight`, and spawns its task; the task waits for a concurrency permit no later than the
deadline. These are the pieces, and the replies a request gets when one of them stops it: code
`busy` when it is not admitted or the service is stopping, `deadline_exceeded` when its deadline
passes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from ..correlation import CorrelationContext
from ..deadline import Deadline
from ..service_config import ServiceConfig
from ..validation import serialize_payload


async def permit_within(sem: asyncio.Semaphore, deadline: Deadline | None) -> bool:
    """Take a permit from `sem`, waiting no later than `deadline`; False when it passed first."""
    bound = asyncio.timeout_at(None if deadline is None else deadline.at)
    try:
        async with bound:
            await sem.acquire()
    except TimeoutError:
        if not bound.expired():
            raise
        return False
    return True


def admission_bound(config: ServiceConfig) -> int | None:
    """The most requests admitted at once on a path: `max_rpc_in_flight`, or none when it is unset.

    Unset is no bound, whatever the concurrency limit, because that is what a burst meets today:
    before the callback, nats-py holds a subscription's pending messages up to its own limit
    (524288 messages or 128 MiB by default), so a service with a concurrency limit queues a burst
    and finishes it. A default derived from the concurrency limit would refuse such a burst.
    """
    return config.max_rpc_in_flight


def admit(admitted: dict[str, int], task: Any, path: str) -> None:
    """Count `task` as admitted on `path` until it is done, however it ends.

    The count is returned by the task's done-callback, which runs exactly once for every task:
    completed, raised, or cancelled before it started.
    """
    admitted[path] += 1

    def finished(_task: Any) -> None:
        admitted[path] -= 1

    task.add_done_callback(finished)


async def answer_busy(
    msg: Any, text: str, logger: Any, *, limit: int | None = None, in_flight: int | None = None
) -> None:
    """Answer `msg` with code `busy`, in JSON, which every caller reads."""
    # Imported here: `rpc` imports this module, and the reply helper lives there.
    from .rpc import answer

    reply: dict[str, Any] = {
        "success": False,
        "error": text,
        "code": "busy",
        "timestamp": datetime.now(UTC).isoformat(),
        "correlation_id": CorrelationContext.extract_from_headers(
            getattr(msg, "headers", None) or {}
        ),
    }
    if limit is not None:
        reply["limit"] = limit
        reply["in_flight"] = in_flight
    data, content_type = serialize_payload(reply, format="json")
    try:
        await answer(msg, data, content_type=content_type, correlation_id=reply["correlation_id"])
    except Exception as e:
        logger.error(f"Failed to send RPC reply: {e}")


def deadline_text(handler_name: str, deadline: Deadline, *, ran: bool = False) -> str:
    """What happened to a request whose deadline passed: cut off, or never started."""
    who = {
        "caller": "set by its caller",
        "service": "max_rpc_processing_time",
        "timer": "set on its timer",
    }[deadline.set_by]
    elapsed = deadline.budget - deadline.remaining()
    if ran:
        return (
            f"{handler_name} exceeded its deadline of {deadline.budget:.3f}s ({who}) and "
            f"was cancelled after {elapsed:.3f}s"
        )
    return (
        f"{handler_name} was not started: its deadline of {deadline.budget:.3f}s ({who}) "
        f"passed after {elapsed:.3f}s waiting"
    )


def deadline_reply(
    handler_name: str, deadline: Deadline, correlation_id: Any, *, ran: bool
) -> dict[str, Any]:
    """The reply to a request cut off at, or not started by, its deadline."""
    return {
        "success": False,
        "error": deadline_text(handler_name, deadline, ran=ran),
        "code": "deadline_exceeded",
        "budget": deadline.budget,
        "elapsed": deadline.budget - deadline.remaining(),
        "set_by": deadline.set_by,
        "timestamp": datetime.now(UTC).isoformat(),
        "correlation_id": correlation_id,
    }


def warn_if_still_running(logger: Any, handler_name: str, deadline: Deadline | None) -> Any:
    """A warning, at twice the budget, that a handler cut off is suppressing its cancel."""
    if deadline is None:
        return None
    return asyncio.get_running_loop().call_at(
        deadline.at + deadline.budget,
        lambda: logger.warning(
            f"{handler_name} is still running {deadline.budget:.3f}s after being "
            f"cancelled at its deadline: it is suppressing the cancellation, so its "
            f"request is not answered until it returns"
        ),
    )
