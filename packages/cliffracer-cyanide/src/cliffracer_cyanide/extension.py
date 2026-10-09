"""CyanideExtension for Cliffracer.

Provides mundane failure mode injection for chaos engineering and reliability testing.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.extension import Extension, WorkerContext
from cliffracer_cyanide.config import (
    FAULT_MODE_NAMES,
    RANDOM_MODE,
    CyanideConfig,
    check_a_mode,
    check_seconds,
    normalize_mode,
)
from cliffracer_cyanide.exceptions import CyanideDisabledError, CyanideFaultError

if TYPE_CHECKING:
    from cliffracer.core.extension import ExtensionSetupContext


@dataclass(frozen=True)
class Injection:
    """One fault, as it was dispatched.

    `correlation_id` is the join key. It is populated by `CorrelationExtension`,
    which every service binds ahead of its declared extensions, so it is set by
    the time cyanide sees the message -- from a header, from a
    `correlation_id` in the payload, or freshly generated. A caller that wants
    to join its own records against these must supply one of the first two;
    a generated id is known only to the service.

    `seed` is the seed IN FORCE WHEN THE FAULT WAS CHOSEN, recorded rather than
    read back later, because a caller may rotate `config.seed` while traffic is
    running and the value at the end of a run does not describe the middle of it.
    """

    correlation_id: str | None
    subject: str | None
    mode: str
    seed: str | int | None


class CyanideExtension(Extension):
    """Extension that injects mundane failures for resilience testing.

    Declared on a service:
        class Orders(CliffracerService):
            cyanide = CyanideExtension(config=CyanideConfig(enabled=True))

    When enabled, failure modes can be triggered directly in handlers or intercepted
    via worker_setup based on configuration or request headers.
    """

    fails_closed: bool = True

    def __init__(
        self,
        config: CyanideConfig | None = None,
        **kwargs: Any,
    ) -> None:
        if config is not None:
            if kwargs:
                cfg_dict = config.model_dump()
                cfg_dict.update(kwargs)
                self.config = CyanideConfig(**cfg_dict)
            else:
                self.config = config
        elif kwargs:
            self.config = CyanideConfig(**kwargs)
        else:
            self.config = CyanideConfig()

        self.active_mode: str | None = self.config.mode
        self._handler_modes: dict[str, str] = {}
        self._injections: deque[Injection] = deque(maxlen=self.config.injection_record_limit)
        self._injections_dropped = 0
        # How many messages with the same subject and payload and no caller-supplied id have been
        # drawn for, so the n-th of them draws the n-th time; see `_compute_random_mode`. Bounded
        # at the size of the injection record, least recently seen first out.
        self._occurrences: OrderedDict[tuple[str, str], int] = OrderedDict()

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        """Store reference to the enclosing service instance."""
        self.service = ctx.service
        if self.config.seed is None:
            self.config.seed = str(uuid.uuid4())
        self._service_log.warning(f"{self.name}: cyanide seeded with '{self.config.seed}'")

    async def start(self) -> None:
        """Start lifecycle hook."""
        if self.config.enabled:
            self._service_log.debug(f"{self.name}: cyanide extension ready (enabled)")

    async def stop(self) -> None:
        """Stop lifecycle hook."""

    def health_details(self) -> dict[str, Any] | None:
        """Report extension status to health check endpoint.

        Counts, not the record itself: the record holds up to
        `injection_record_limit` entries and a health payload is read on a
        schedule. `injections_dropped` is here rather than only on the
        extension because it is the number that says whether the record can be
        trusted to be complete.
        """
        return {
            "enabled": self.config.enabled,
            "mode": self.active_mode,
            "seed": self.config.seed,
            "injections_recorded": len(self._injections),
            "injections_dropped": self._injections_dropped,
            "injection_record_limit": self._injections.maxlen,
        }

    def info_details(self) -> dict[str, Any] | None:
        """Report extension information to info endpoint."""
        return {
            "enabled": self.config.enabled,
            "mode": self.active_mode,
            "seed": self.config.seed,
        }

    def injections(self) -> list[Injection]:
        """Every fault still remembered, oldest first, without consuming them."""
        return list(self._injections)

    def drain_injections(self) -> list[Injection]:
        """Return the remembered faults and forget them.

        The drop count is NOT reset: it describes the run, and a caller that
        drains periodically still needs to know the record was ever incomplete.
        """
        drained = list(self._injections)
        self._injections.clear()
        return drained

    @property
    def injections_dropped(self) -> int:
        """Faults evicted by the bound since this extension was constructed.

        Non-zero means the record is incomplete, so "no injection recorded for
        this message" stops being evidence that the message was not faulted. A
        caller joining against the record must read this and refuse to conclude
        rather than report an unexplained failure.
        """
        return self._injections_dropped

    def _record(self, ctx: WorkerContext, mode: str) -> None:
        """Remember one dispatched fault, dropping the oldest if the bound is met."""
        if len(self._injections) == self._injections.maxlen:
            self._injections_dropped += 1
        self._injections.append(
            Injection(
                correlation_id=ctx.correlation_id,
                subject=ctx.subject,
                mode=mode,
                seed=self.config.seed,
            )
        )

    def set_mode(self, mode: str | None) -> None:
        """Set or clear the active global failure mode.

        Raises `ValueError` for a name that is not a mode, as the config does.
        """
        self.active_mode = check_a_mode(mode)

    def configure_handler(self, handler_name: str, mode: str) -> None:
        """Map a specific handler name to a failure mode.

        Raises `ValueError` for a name that is not a mode, as the config does.
        """
        if not check_a_mode(mode):
            raise ValueError("a handler's cyanide mode cannot be empty")
        self._handler_modes[handler_name] = mode

    def set_enabled(self, enabled: bool) -> None:
        """Dynamically enable or disable fault injection."""
        self.config.enabled = enabled

    async def slow(self, delay: float | None = None) -> None:
        """Delay execution for a configured duration."""
        if not self.config.enabled:
            raise CyanideDisabledError("Cyanide failure modes are disabled")
        d = check_seconds(self.config.slow_delay if delay is None else delay, "delay")
        await asyncio.sleep(d)

    async def raise_after_delay(
        self,
        delay: float | None = None,
        message: str = "Injected cyanide failure",
    ) -> None:
        """Delay execution and then raise a typed CyanideFaultError."""
        if not self.config.enabled:
            raise CyanideDisabledError("Cyanide failure modes are disabled")
        d = check_seconds(self.config.raise_delay if delay is None else delay, "delay")
        await asyncio.sleep(d)
        raise CyanideFaultError(message)

    async def sleep_past_timeout(self, duration: float | None = None) -> None:
        """Sleep past standard caller timeout to test cancellation handling."""
        if not self.config.enabled:
            raise CyanideDisabledError("Cyanide failure modes are disabled")
        dur = check_seconds(
            self.config.sleep_timeout_duration if duration is None else duration, "duration"
        )
        await asyncio.sleep(dur)

    async def drop_reply(self, ctx: WorkerContext) -> None:
        """Suppress sending wire reply on NATS reply subject."""
        if not self.config.enabled:
            raise CyanideDisabledError("Cyanide failure modes are disabled")

        async def _noop_respond(*args: Any, **kwargs: Any) -> None:
            pass

        if ctx.raw is not None and hasattr(ctx.raw, "respond"):
            ctx.raw.respond = _noop_respond
        ctx.data["_cyanide_dropped_reply"] = True

    def _resolve_mode(self, ctx: WorkerContext) -> tuple[str | None, dict[str, str]]:
        """Determine if a failure mode is requested for this worker context."""
        headers: dict[str, str] = {k.lower(): v for k, v in (ctx.headers or {}).items()}

        # Check headers first
        mode = headers.get("x-cyanide-mode") or headers.get("x-cyanide-fault")

        # Check per-handler configuration
        if not mode:
            handler_name = ctx.data.get("handler_name")
            if handler_name and handler_name in self._handler_modes:
                mode = self._handler_modes[handler_name]
            elif ctx.subject:
                parts = ctx.subject.split(".")
                if parts[-1] in self._handler_modes:
                    mode = self._handler_modes[parts[-1]]

        # Fall back to active mode or config mode
        if not mode:
            mode = self.active_mode

        return mode, headers

    @staticmethod
    def _caller_id(ctx: WorkerContext) -> str | None:
        """The correlation id the caller sent with the message, or None when the framework made it.

        `CorrelationExtension` always fills `ctx.correlation_id`, with a fresh id when the message
        carried none, so the field cannot say which it is. The id counts as the caller's when the
        message's own headers or payload carry the same one.
        """
        sent = CorrelationContext.extract_from_headers(ctx.headers)
        if sent is None and isinstance(ctx.payload, dict):
            sent = ctx.payload.get("correlation_id")
        if isinstance(sent, str) and sent and sent == ctx.correlation_id:
            return sent
        return None

    def _occurrence(self, subject: str, digest: str) -> int:
        """How many messages with this subject and payload were drawn for before this one."""
        pair = (subject, digest)
        seen = self._occurrences.pop(pair, -1) + 1
        self._occurrences[pair] = seen
        while len(self._occurrences) > self._injections.maxlen:  # type: ignore[operator]
            self._occurrences.popitem(last=False)
        return seen

    def _compute_random_mode(self, ctx: WorkerContext) -> str | None:
        """Determine failure mode from the seed, the message and the weights.

        The draw is a pure function of the seed and what identifies the message, so a run with the
        same seed injects the same faults. A message is identified by the correlation id its
        caller sent. A message with none is identified by its subject and payload and by how many
        identical ones came before it: messages the framework gave a fresh id are otherwise
        indistinguishable between runs, and identical messages given one draw would all be
        faulted, or none. They are interchangeable, so which of them gets the n-th draw does not
        change which faults a run injects. The count is kept for the last `injection_record_limit`
        distinct subject and payload pairs; a pair older than that starts again at the first draw.
        """
        subj = ctx.subject or ""
        caller_id = self._caller_id(ctx)

        if caller_id is not None:
            identity = f"id:{caller_id}"
        else:
            import json

            try:
                digest = hashlib.sha256(
                    json.dumps(
                        ctx.payload,
                        sort_keys=True,
                        default=lambda o: f"{type(o).__module__}.{type(o).__qualname__}",
                    ).encode()
                ).hexdigest()
            except Exception:
                # Fallback for completely un-stringifiable payloads
                digest = "unhashable_payload"
            identity = f"anon:{digest}:{self._occurrence(subj, digest)}"

        seed_str = str(self.config.seed)
        h = hashlib.sha256(f"{seed_str}:{subj}:{identity}".encode()).hexdigest()
        val = int(h[:8], 16) / 0xFFFFFFFF

        w_slow = self.config.slow_weight
        w_drop = self.config.drop_reply_weight
        w_raise = self.config.raise_after_delay_weight
        w_sleep = self.config.sleep_past_timeout_weight

        if val < w_slow:
            return "slow"
        val -= w_slow
        if val < w_drop:
            return "drop_reply"
        val -= w_drop
        if val < w_raise:
            return "raise_after_delay"
        val -= w_raise
        if val < w_sleep:
            return "sleep_past_timeout"

        return None

    def _header_seconds(self, headers: dict[str, str], name: str) -> float | None:
        """The seconds a request header asks for, or None for the configured value.

        A header is the caller's text: one that is not a finite number of seconds, 0 or more, is
        logged and ignored, as a header naming no mode is, and the fault runs with the configured
        value. Refusing the request for it would let a caller fail the service's own traffic.
        """
        raw = headers.get(name)
        if raw is None:
            return None
        try:
            return check_seconds(float(raw), name)
        except ValueError:
            self._service_log.warning(
                f"{self.name}: ignoring header {name}={raw!r}: not a number of seconds"
            )
            return None

    async def worker_setup(self, ctx: WorkerContext) -> None:
        """Intercept inbound messages and execute configured failure mode."""
        mode, headers = self._resolve_mode(ctx)
        if not mode:
            return

        if not self.config.enabled:
            self._service_log.warning(
                f"{self.name}: cyanide mode {mode!r} requested while disabled; ignoring"
            )
            return

        normalized_mode = normalize_mode(mode)

        if normalized_mode == RANDOM_MODE:
            random_mode = self._compute_random_mode(ctx)
            if not random_mode:
                return
            normalized_mode = random_mode

        # Ensure we're executing a valid mapped mode before we log
        if normalized_mode in FAULT_MODE_NAMES:
            # Recorded here, under the condition that already gates the log line
            # and BEFORE the fault runs. `raise_after_delay` raises, so a record
            # written afterwards would never exist for the one mode a caller
            # most needs to attribute. One point, one condition, so the record
            # and the log cannot disagree about what was injected.
            self._record(ctx, normalized_mode)
            self._service_log.info(
                f"Injecting fault: mode={normalized_mode} "
                f"subject={ctx.subject or 'unknown'} "
                f"correlation_id={ctx.correlation_id or 'unknown'} "
                f"seed={self.config.seed}"
            )

        if normalized_mode == "slow":
            await self.slow(delay=self._header_seconds(headers, "x-cyanide-delay"))

        elif normalized_mode in ("raise_after_delay", "raise_delay", "raise", "fault"):
            msg = headers.get("x-cyanide-message", "Injected cyanide failure")
            await self.raise_after_delay(
                delay=self._header_seconds(headers, "x-cyanide-delay"), message=msg
            )

        elif normalized_mode in ("sleep_past_timeout", "timeout", "sleep_timeout"):
            await self.sleep_past_timeout(
                duration=self._header_seconds(headers, "x-cyanide-duration")
            )

        elif normalized_mode in ("drop_reply", "drop"):
            await self.drop_reply(ctx)

        else:
            self._service_log.warning(f"{self.name}: unrecognized cyanide mode '{mode}'")


__all__ = ["CyanideExtension", "Injection"]
