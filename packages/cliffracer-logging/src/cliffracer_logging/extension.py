"""LoggingExtension: NATS log streaming and per-dispatch timing on the hook chain."""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from cliffracer.core.extension import Extension, ExtensionSetupContext, WorkerContext

from .config import LoggingConfig


class LoggingExtension(Extension):
    """Stream service logs to NATS and record dispatch timing on the hook chain.

    class Orders(CliffracerService):
        logging = LoggingExtension(to_nats=True)
    """

    def __init__(self, *, to_nats: bool = False, timing: bool = True) -> None:
        self.to_nats = to_nats
        self.timing = timing
        # Instance state initialized in setup() for per-service isolation.
        self._sink_id: int | None = None

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        self._sink_id = None

    async def start(self) -> None:
        if not self.to_nats:
            return
        nc = getattr(self.service, "nc", None)
        if nc is None:
            logger.warning(
                f"{self.name}: to_nats is set but the service has no NATS "
                "connection; logs are not being streamed"
            )
            return
        self._sink_id = LoggingConfig.add_nats_sink(
            service_name=self.service.config.name,
            nats_connection=nc,
            log_level=self.service.config.log_level,
        )

    async def stop(self) -> None:
        # Remove registered NATS sink handler on extension shutdown.
        if self._sink_id is None:
            return
        try:
            logger.remove(self._sink_id)
        except ValueError:
            # Already removed -- another sink teardown, or logger.remove() with
            # no argument somewhere. Not worth failing a shutdown over.
            pass
        finally:
            self._sink_id = None

    async def worker_setup(self, ctx: WorkerContext) -> None:
        if self.timing:
            # ctx.data is per-dispatch scratch space; an
            # attribute on self would be overwritten by concurrent dispatches.
            ctx.data["_logging_t0"] = time.perf_counter()

    async def worker_result(
        self, ctx: WorkerContext, result: object | None, exc: BaseException | None
    ) -> None:
        t0 = ctx.data.pop("_logging_t0", None)
        if t0 is None:
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.debug(f"{ctx.kind} {ctx.subject} {elapsed_ms:.1f}ms")

    def health_details(self) -> dict[str, Any] | None:
        return {"to_nats": self.to_nats, "streaming": self._sink_id is not None}
