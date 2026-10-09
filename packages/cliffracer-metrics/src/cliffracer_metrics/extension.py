"""Dispatch timing and throughput metrics extension."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any

from cliffracer.core.extension import Extension, ExtensionSetupContext, RejectMessage, WorkerContext

# Latency samples kept per entrypoint kind. Bounded because a long-running
# service dispatches without limit and this buffer is read on every /health.
_LATENCY_WINDOW = 1000


class MetricsExtension(Extension):
    """Execution metrics extension recording dispatch latency and counts per dispatch kind.

    Hooks into worker_setup and worker_result to count dispatches, errors, rejections and
    cancellations, and to time them, for /health reporting. Everything is kept per dispatch kind
    (`rpc`, `async_rpc`, `event`, `timer`): fifty RPC handlers share one count and one latency
    window, because the key is the kind and not the handler.

    A listener the service paused because a dependency it names in `pause_when_down` is down reads
    1 under `listener_paused`, by subject, and 0 once it is resumed.

    A dispatch that an extension refused (`RejectMessage`) is counted as `rejected`, one that was
    cancelled (a shutdown cancelling in-flight handlers, a timeout cancelling a handler) as
    `cancelled`, and any other exception as an `error`; none of the three is counted as
    another. The refusal the pipeline makes when a `fails_closed` extension's hook raises is not
    one an extension authored: it is the service failing, and is an `error`.
    """

    def __init__(self) -> None:
        # DECLARED here, CREATED in setup(), where per-service state begins.
        # bind() runs __init__ again for each service, so defaultdicts built
        # here would not be shared either; setup() is the one place every
        # extension's per-service state starts.
        # test_two_services_do_not_share_metrics drives a dispatch through one
        # service and requires the other to have counted nothing.
        self._count: defaultdict[str, int] | None = None
        self._errors: defaultdict[str, int] | None = None
        self._rejected: defaultdict[str, int] | None = None
        self._cancelled: defaultdict[str, int] | None = None
        self._latency: defaultdict[str, deque[float]] | None = None
        # 1 while the service has stopped consuming a listener because a dependency it names in
        # `pause_when_down` is down, 0 once it consumes again, by subject. Only listeners that
        # have been paused at least once appear.
        self._listener_paused: dict[str, int] | None = None

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        self._count = defaultdict(int)
        self._errors = defaultdict(int)
        self._rejected = defaultdict(int)
        self._cancelled = defaultdict(int)
        self._latency = defaultdict(lambda: deque(maxlen=_LATENCY_WINDOW))
        self._listener_paused = {}

    async def on_listener_paused(self, subject: str, dependencies: tuple[str, ...]) -> None:
        if self._listener_paused is not None:
            self._listener_paused[subject] = 1

    async def on_listener_resumed(self, subject: str, dependencies: tuple[str, ...]) -> None:
        if self._listener_paused is not None:
            self._listener_paused[subject] = 0

    async def worker_setup(self, ctx: WorkerContext) -> None:
        # ctx.data is per-dispatch scratch space, so
        # concurrent dispatches cannot overwrite each other's start time the
        # way an attribute on self would.
        ctx.data["_metrics_t0"] = time.perf_counter()

    async def worker_result(
        self, ctx: WorkerContext, result: object | None, exc: BaseException | None
    ) -> None:
        if (
            self._count is None
            or self._errors is None
            or self._rejected is None
            or self._cancelled is None
            or self._latency is None
        ):
            return
        t0 = ctx.data.pop("_metrics_t0", None)
        self._count[ctx.kind] += 1
        if (isinstance(exc, RejectMessage) and not exc.hook_crash) or (
            exc is None and ctx.data.get("outcome") == "invalid"
        ):
            # A refusal an extension authored is a policy decision, tracked apart from errors.
            # So is an event the dispatcher found invalid: it answers that with a dead letter and
            # raises nothing, and marks the dispatch `ctx.data["outcome"] = "invalid"`, where an
            # invalid RPC raises a refusal.
            # The refusal the pipeline synthesises when a `fails_closed` hook raises (an auth
            # backend that is down, a validator that crashes) is the service being broken, which
            # the wire reports as `internal`: it falls through to the error count below.
            self._rejected[ctx.kind] += 1
        elif isinstance(exc, asyncio.CancelledError):
            # Not a handler fault: the dispatch was stopped from outside.
            self._cancelled[ctx.kind] += 1
        elif exc is not None:
            self._errors[ctx.kind] += 1
        if t0 is not None:
            self._latency[ctx.kind].append((time.perf_counter() - t0) * 1000.0)

    def health_details(self) -> dict[str, Any] | None:
        # No contribution before setup(): the counters do not exist yet, and
        # "not set up" is not an error worth reporting on /health.
        if (
            self._count is None
            or self._latency is None
            or self._errors is None
            or self._rejected is None
            or self._cancelled is None
        ):
            return None
        out: dict[str, Any] = {}
        for kind, n in self._count.items():
            lat = self._latency[kind] or (0.0,)
            out[kind] = {
                "count": n,
                "errors": self._errors[kind],
                "rejected": self._rejected[kind],
                "cancelled": self._cancelled[kind],
                "latency_ms": {"max": max(lat), "avg": sum(lat) / len(lat)},
            }
        if self._listener_paused:
            out["listener_paused"] = dict(sorted(self._listener_paused.items()))
        return out
