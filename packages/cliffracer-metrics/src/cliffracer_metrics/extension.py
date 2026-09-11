"""Dispatch timing and throughput metrics extension."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from cliffracer.core.extension import Extension, ExtensionSetupContext, RejectMessage, WorkerContext

# Latency samples kept per entrypoint kind. Bounded because a long-running
# service dispatches without limit and this list is read on every /health.
_LATENCY_WINDOW = 1000


class MetricsExtension(Extension):
    """Execution metrics extension recording handler latency and call counts.

    Hooks into worker_setup and worker_result to track per-handler execution duration,
    throughput, and rejection errors for /health reporting.
    """

    def __init__(self) -> None:
        # DECLARED here, CREATED in setup(). bind() is a SHALLOW copy, so
        # defaultdicts built in __init__ are the SAME objects in every bound
        # copy: two services would pool their counts and latencies into one
        # set of numbers, reported on both /health endpoints. None is
        # immutable, so sharing these placeholders costs nothing.
        # Pinned by test_two_services_do_not_share_metrics, the analogue of
        # HttpExtension's test_two_services_do_not_share_websocket_state.
        self._count: defaultdict[str, int] | None = None
        self._errors: defaultdict[str, int] | None = None
        self._rejected: defaultdict[str, int] | None = None
        self._latency: defaultdict[str, list[float]] | None = None

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        self._count = defaultdict(int)
        self._errors = defaultdict(int)
        self._rejected = defaultdict(int)
        self._latency = defaultdict(list)

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
            or self._latency is None
        ):
            return
        t0 = ctx.data.pop("_metrics_t0", None)
        self._count[ctx.kind] += 1
        if isinstance(exc, RejectMessage):
            # Policy rejections are tracked separately from operational errors.
            self._rejected[ctx.kind] += 1
        elif exc is not None:
            self._errors[ctx.kind] += 1
        if t0 is not None:
            bucket = self._latency[ctx.kind]
            bucket.append((time.perf_counter() - t0) * 1000.0)
            del bucket[:-_LATENCY_WINDOW]

    def health_details(self) -> dict[str, Any] | None:
        # No contribution before setup(): the counters do not exist yet, and
        # "not set up" is not an error worth reporting on /health.
        if (
            self._count is None
            or self._latency is None
            or self._errors is None
            or self._rejected is None
        ):
            return None
        out: dict[str, Any] = {}
        for kind, n in self._count.items():
            lat = self._latency[kind] or [0.0]
            out[kind] = {
                "count": n,
                "errors": self._errors[kind],
                "rejected": self._rejected[kind],
                "latency_ms": {"max": max(lat), "avg": sum(lat) / len(lat)},
            }
        return out
