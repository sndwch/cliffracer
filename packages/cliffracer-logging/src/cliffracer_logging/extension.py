"""LoggingExtension: NATS log streaming and per-dispatch timing on the hook chain."""

from __future__ import annotations

import time
from functools import partial
from typing import Any

from loguru import logger

from cliffracer.core.credentials import credential_names_of
from cliffracer.core.extension import Extension, WorkerContext

from .config import LoggingConfig, LogRecordRedactor, NatsSinkStats, redact_sensitive_log_fields


class LoggingExtension(Extension):
    """Stream service logs to NATS and record dispatch timing on the hook chain.

    class Orders(CliffracerService):
        logging = LoggingExtension(to_nats=True)
    """

    def __init__(
        self,
        *,
        to_nats: bool = False,
        timing: bool = True,
        log_level: str = "INFO",
        redactor: LogRecordRedactor = redact_sensitive_log_fields,
    ) -> None:
        self.to_nats = to_nats
        self.timing = timing
        # The NATS sink belongs to this extension and publishes only the records bound to this
        # service, so its level is the extension's own setting.
        self.log_level = log_level
        self.redactor = redactor
        # Per-service state: `bind()` builds a new instance for each service, so this is not shared.
        self._sink_id: int | None = None
        self._sink_stats: NatsSinkStats | None = None

    @property
    def _log(self) -> Any:
        """The loguru logger bound to this service, which is what its NATS sink publishes."""
        name = getattr(getattr(self.service, "config", None), "name", None)
        return logger.bind(service=name) if name else logger

    async def start(self) -> None:
        if not self.to_nats:
            return
        nc = getattr(self.service, "nc", None)
        if nc is None:
            self._log.warning(
                f"{self.name}: to_nats is set but the service has no NATS "
                "connection; logs are not being streamed"
            )
            return
        redactor = self.redactor
        if redactor is redact_sensitive_log_fields:
            # The default redactor also redacts the header an installed extension reads a
            # credential from (`AuthExtension(header=...)`), which only this service knows.
            container = getattr(self.service, "container", None)
            names = credential_names_of(getattr(container, "extensions", None))
            if names:
                redactor = partial(redact_sensitive_log_fields, credential_names=names)
        self._sink_stats = NatsSinkStats()
        self._sink_id = LoggingConfig.add_nats_sink(
            service_name=self.service.config.name,
            nats_connection=nc,
            config=self.service.config,
            log_level=self.log_level,
            redactor=redactor,
            stats=self._sink_stats,
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
        # A timer has no subject: its method name is on the context instead. The outcome says
        # whether the handler raised, so slow-and-failing can be told from slow-and-fine.
        label = ctx.subject or ctx.data.get("handler_name") or "<unnamed>"
        outcome = "ok" if exc is None else f"failed={type(exc).__name__}"
        self._log.debug(f"{ctx.kind} {label} {elapsed_ms:.1f}ms {outcome}")

    def health_details(self) -> dict[str, Any] | None:
        # Whether the sink is attached, which only loguru's own table knows: the id is
        # remembered, but logger.remove() with no argument (LoggingConfig.configure
        # calls it) detaches every handler without telling the extension.
        attached = self._sink_id is not None and self._sink_id in logger._core.handlers  # type: ignore[attr-defined]
        details: dict[str, Any] = {"to_nats": self.to_nats, "streaming": attached}
        if self._sink_stats is not None:
            # What the sink has published, lost and has in flight: `streaming` says the
            # sink is attached, this says whether anything is getting through it.
            details["nats_sink"] = self._sink_stats.snapshot()
        return details
