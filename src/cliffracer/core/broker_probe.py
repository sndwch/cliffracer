"""A bounded, cached round trip to the broker, for the readiness check.

`nats_connected` and the connection states come from nats-py's own flag, which stays True for
minutes after a connection goes silent without being reset: a partition that drops packets is seen
only when nats-py's ping loop gives up. The readiness check therefore asks the broker.

One probe is a PING and its PONG on the existing connection, bounded by `timeout`. The result,
including a failure, is reused for `cache_seconds`, and callers that arrive while a probe is in
flight share it, so a burst of readiness requests costs one round trip.

THE PONG FUTURE IS NEVER CANCELLED. nats-py matches each PONG to the oldest future in its queue
and resolves it with `set_result`. A future that was cancelled while it waited (which is what
`wait_for(nc.flush(), t)` does on a timeout, and what `flush` itself does at its own) is still at
the head of that queue, and the first PONG after the path heals raises `InvalidStateError` inside
nats-py's read loop. The loop ends without telling the client: `is_connected` stays True and
nothing the broker sends is read again. So the probe makes its own future, hands it to nats-py's
`_send_ping`, and waits with `asyncio.wait`, which cancels nothing. A probe that is not answered
leaves its future in the queue to be resolved, harmlessly, by a later PONG.

Only the probe's OWN future answers it. TCP delivers the PONGs in the order the PINGs were sent, so
an earlier probe's PONG resolves its own future first and the new probe's later; a PONG that
resolves an earlier probe's future is not an answer to this one. Counting it would make a broker
that always takes longer than the bound read healthy part of the time, and let the reply to an old
PING stand in for the new one. After a heal the held PONGs arrive in order and the new probe is
answered last, which is the right answer. The futures a probe leaves behind are kept so they can be
capped, and the ones a reconnect dropped from nats-py's queue are forgotten.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

#: Probes sent and not yet answered, past which none is sent: a partition that lasts keeps each
#: one in nats-py's queue until a PONG arrives, and the queue should not grow with it.
MAX_UNANSWERED = 32

_warned_unavailable = False


@dataclass(frozen=True)
class ProbeResult:
    """What one round trip found: whether it answered in time, and how long it took."""

    ok: bool
    rtt_ms: float | None


def can_ping(nc: Any) -> bool:
    """Whether the client has the hook this probe sends its PING through."""
    return callable(getattr(nc, "_send_ping", None))


class BrokerProbe:
    """Asks the broker whether it is answering, at most once per `cache_seconds`."""

    def __init__(
        self,
        timeout: float | None,
        cache_seconds: float,
        *,
        service: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._service = service
        self._clock = clock
        self._result: ProbeResult | None = None
        self._at = 0.0
        self._inflight: asyncio.Future[ProbeResult] | None = None
        self._unanswered: list[asyncio.Future[Any]] = []

    @property
    def enabled(self) -> bool:
        """`None` for the timeout turns the probe off: readiness reads nats-py's flag only."""
        return self.timeout is not None

    async def check(self, nc: Any) -> ProbeResult | None:
        """The cached result if it is fresh, else one shared round trip.

        None when the client cannot be asked, so readiness falls back to its flag.
        """
        assert self.timeout is not None, "check() called on a probe that is turned off"
        if not can_ping(nc):
            self._say_unavailable()
            return None
        if self._result is not None and self._clock() - self._at < self.cache_seconds:
            return self._result
        if self._inflight is None:
            self._inflight = asyncio.ensure_future(self._round_trip(nc, self.timeout))
        # Shielded: one caller going away must not cancel the probe the others are waiting on.
        return await asyncio.shield(self._inflight)

    def _forget_the_dropped(self, nc: Any) -> None:
        """Drop outstanding futures nats-py no longer holds: a reconnect empties its queue."""
        queue = getattr(nc, "_pongs", None)
        if isinstance(queue, list):
            self._unanswered = [f for f in self._unanswered if not f.done() and f in queue]
        else:
            self._unanswered = [f for f in self._unanswered if not f.done()]

    async def _round_trip(self, nc: Any, timeout: float) -> ProbeResult:
        began = time.perf_counter()
        deadline = began + timeout
        try:
            self._forget_the_dropped(nc)
            if len(self._unanswered) >= MAX_UNANSWERED:
                raise TimeoutError(f"{len(self._unanswered)} earlier round trips are unanswered")
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            # Cancelling the SEND is safe: it is the future that must never be cancelled.
            await asyncio.wait_for(nc._send_ping(future), timeout=timeout)
            self._unanswered.append(future)
            await asyncio.wait([future], timeout=max(deadline - time.perf_counter(), 0.0))
            self._unanswered = [f for f in self._unanswered if not f.done()]
            if not future.done() or future.cancelled() or future.exception() is not None:
                raise TimeoutError(f"did not answer within {timeout:g} seconds")
            result = ProbeResult(True, round((time.perf_counter() - began) * 1000, 2))
        except TimeoutError as exc:
            result = ProbeResult(False, None)
            self._say(
                result,
                str(exc)
                if "unanswered" in str(exc)
                else f"did not answer within {timeout:g} seconds",
            )
        except Exception as exc:  # noqa: BLE001 - a failed probe is a result, not a crash
            result = ProbeResult(False, None)
            self._say(result, f"failed: {type(exc).__name__}: {exc}")
        else:
            self._say(result, "answered")
        finally:
            self._inflight = None
        self._result, self._at = result, self._clock()
        return result

    def _say(self, result: ProbeResult, what: str) -> None:
        """Log a change of answer, not every probe: a healthy service says nothing."""
        before = self._result
        if (before is None or before.ok) and not result.ok:
            logger.bind(service=self._service).warning(
                f"Service '{self._service}' broker round trip {what}; reporting disconnected"
            )
        elif before is not None and not before.ok and result.ok:
            logger.bind(service=self._service).info(
                f"Service '{self._service}' broker round trip {what} again"
            )

    def _say_unavailable(self) -> None:
        global _warned_unavailable
        if not _warned_unavailable:
            _warned_unavailable = True
            logger.bind(service=self._service).warning(
                f"Service '{self._service}' cannot ask the broker for a round trip: the client has "
                "no `_send_ping`, so readiness reads its connection flag alone"
            )
