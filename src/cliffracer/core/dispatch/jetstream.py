"""JetStream message handling, transport protections, and pull consumers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from loguru import logger as global_logger

from ..jetstream import (
    consumer_config_drift,
    consumer_config_for,
    nak_delay,
)
from ..service_config import ServiceConfig
from .dlq import DeadLetterPublisher
from .events import DispatchOutcome, EventDispatcher


class _JetStreamHeartbeat:
    """An asynchronous context manager pulsing in-progress status for active JetStream messages.

    Invariants:
    - Runs a background task calling safe_in_progress every interval seconds.
    - Cancels and awaits the background heartbeat task upon exit.
    - Never raises an exception if message pulsing fails or if the message is unsupported.
    """

    def __init__(self, dispatcher: Any, msg: Any, interval: float) -> None:
        self._dispatcher = dispatcher
        self._msg = msg
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _JetStreamHeartbeat:
        if (
            self._interval > 0
            and hasattr(self._msg, "in_progress")
            and callable(self._msg.in_progress)
        ):
            self._task = asyncio.create_task(self._pulse_loop(), name="jetstream_heartbeat")
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger = getattr(self._dispatcher, "logger", None)
                if logger:
                    logger.debug(f"Heartbeat pulse task exit error: {exc}")

    async def _pulse_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            pulse_fn = getattr(self._dispatcher, "safe_in_progress", None) or getattr(
                self._dispatcher, "_safe_in_progress", None
            )
            if pulse_fn:
                await pulse_fn(self._msg)


class JetStreamDispatcher:
    """JetStream transport operations, pull consumers, and heartbeats.

    Invariants:
    - Genuine transport protections with safe ACK/NAK/TERM/in-progress.
    - Eliminates circular container.__dict__ checks.
    - Pulses heartbeat during long-running event processing.
    - Auto-unsubscribes pull consumers upon graceful loop exit.
    """

    def __init__(
        self,
        config: ServiceConfig,
        connection_provider: Callable[[], Any],
        event_dispatcher: EventDispatcher,
        dlq: DeadLetterPublisher,
        task_spawner: Callable[[Coroutine[Any, Any, Any], str | None], asyncio.Task[Any]]
        | None = None,
        logger: Any = None,
        service: Any = None,
    ) -> None:
        self.config = config
        self.connection_provider = connection_provider
        self.events = event_dispatcher
        self.dlq = dlq
        self.task_spawner = task_spawner
        self.logger = logger or global_logger.bind(service=config.name)
        self.service = service

    @property
    def nc(self) -> Any:
        conn = self.connection_provider()
        return getattr(conn, "nc", None)

    @property
    def js(self) -> Any:
        conn = self.connection_provider()
        return getattr(conn, "js", None)

    @property
    def _jetstream_active(self) -> bool:
        conn = self.connection_provider()
        return getattr(conn, "jetstream_active", False)

    def _spawn_task(
        self, coro: Coroutine[Any, Any, Any], name: str | None = None
    ) -> asyncio.Task[Any]:
        if self.task_spawner is not None:
            return self.task_spawner(coro, name)
        return asyncio.create_task(coro, name=name)

    async def safe_ack(self, msg: Any) -> bool:
        """Safely acknowledge a JetStream message."""
        try:
            if hasattr(msg, "ack") and callable(msg.ack):
                await msg.ack()
                return True
        except Exception as exc:
            self.logger.warning(
                f"Failed to ACK JetStream message on '{getattr(msg, 'subject', '')}': {exc}"
            )
        return False

    async def safe_nak(self, msg: Any, delay: float = 0.0) -> bool:
        """Safely negatively acknowledge a JetStream message with optional backoff delay."""
        try:
            if hasattr(msg, "nak") and callable(msg.nak):
                await msg.nak(delay=delay)
                return True
        except Exception as exc:
            self.logger.warning(
                f"Failed to NAK JetStream message on '{getattr(msg, 'subject', '')}': {exc}"
            )
        return False

    async def safe_term(self, msg: Any) -> bool:
        """Safely terminate a JetStream message to prevent redelivery."""
        try:
            if hasattr(msg, "term") and callable(msg.term):
                await msg.term()
                return True
        except Exception as exc:
            self.logger.warning(
                f"Failed to TERM JetStream message on '{getattr(msg, 'subject', '')}': {exc}"
            )
        return False

    async def safe_in_progress(self, msg: Any) -> bool:
        """Safely pulse in-progress heartbeat for an active JetStream message."""
        try:
            if hasattr(msg, "in_progress") and callable(msg.in_progress):
                await msg.in_progress()
                return True
        except Exception as exc:
            self.logger.debug(
                f"Failed to send in-progress pulse for JetStream message on '{getattr(msg, 'subject', '')}': {exc}"
            )
        return False

    def make_event_callback(self, pattern: str) -> Callable[[Any], Awaitable[None]]:
        """Construct a NATS message callback dispatching JetStream events for a pattern."""

        async def _cb(msg: Any) -> None:
            sem = self.events.get_event_semaphore()
            if sem is not None:
                await sem.acquire()
                self._spawn_task(
                    self._bounded_handle_jetstream_event(msg, pattern, sem),
                    name=f"jetstream_event_bounded:{pattern}",
                )
            else:
                self._spawn_task(
                    self.handle_jetstream_event(msg, pattern=pattern),
                    name=f"jetstream_event:{pattern}",
                )

        return _cb

    async def _bounded_handle_jetstream_event(
        self, msg: Any, pattern: str, sem: asyncio.Semaphore
    ) -> None:
        try:
            await self.handle_jetstream_event(msg, pattern=pattern)
        finally:
            sem.release()

    async def handle_jetstream_event(self, msg: Any, *, pattern: str | None = None) -> None:
        """JetStream event dispatch with pulse heartbeat, ack, nak, or termination."""
        ack_wait = getattr(self.config, "jetstream_ack_wait", 30.0) or 30.0
        pulse_interval = max(0.05, ack_wait / 2.0) if ack_wait > 0 else 0.0

        try:
            async with _JetStreamHeartbeat(self, msg, pulse_interval):
                outcome = await self.events.handle_event(msg, pattern=pattern, raise_on_error=True)
        except Exception as error:
            num_delivered = getattr(getattr(msg, "metadata", None), "num_delivered", 1)
            if num_delivered >= self.config.jetstream_max_deliver:
                await self.dlq.dead_letter_terminated(msg, error, num_delivered)
                await self.safe_term(msg)
            else:
                await self.safe_nak(msg, delay=nak_delay(num_delivered, self.config))
            return

        if outcome is DispatchOutcome.INVALID:
            await self.safe_term(msg)
            return

        await self.safe_ack(msg)

    async def pull_once(self, sub: Any, *, pattern: str | None = None) -> int:
        """Fetch one batch from a JetStream pull consumer and dispatch messages."""
        try:
            msgs = await sub.fetch(
                self.config.jetstream_pull_batch, timeout=self.config.jetstream_pull_timeout
            )
        except TimeoutError:
            return 0
        except Exception as exc:
            if type(exc).__name__ == "TimeoutError":
                return 0
            raise

        for msg in msgs:
            task = self._spawn_task(
                self.handle_jetstream_event(msg, pattern=pattern)
                if pattern is not None
                else self.handle_jetstream_event(msg),
                name="jetstream_pull_event",
            )
            await asyncio.shield(task)
        return len(msgs)

    async def pull_loop(
        self,
        sub: Any,
        durable: str,
        *,
        pattern: str | None = None,
        is_running_fn: Callable[[], bool] | None = None,
    ) -> None:
        """Continually fetch batches from a JetStream pull consumer while running."""
        try:
            while is_running_fn() if is_running_fn else True:
                try:
                    count = await self.pull_once(sub, pattern=pattern)
                    if count == 0:
                        await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.error(f"pull loop for durable {durable!r} failed: {exc}")
                    await asyncio.sleep(self.config.jetstream_nak_backoff)
        finally:
            if (
                self.nc
                and not getattr(self.nc, "is_draining", False)
                and not getattr(self.nc, "is_closed", False)
            ):
                try:
                    await sub.unsubscribe()
                except Exception:
                    pass

    async def report_consumer_drift(self, sub: Any, durable: str) -> None:
        """Warn when server consumer configuration diverges from service configuration."""
        try:
            info = await sub.consumer_info()
            drift = consumer_config_drift(consumer_config_for(self.config), info.config)
        except Exception as exc:
            self.logger.debug(f"could not read consumer info for durable {durable!r}: {exc}")
            return

        if not drift:
            return

        fields = "; ".join(
            f"{name}={actual!r}, not the {want!r} asked for" for name, want, actual in drift
        )
        log = getattr(self.service, "logger", self.logger)
        log.warning(
            f"durable {durable!r} runs with {fields}. A durable's config is fixed "
            f"when it is created and a later subscribe does not update it. To "
            f"apply the new tuning: nats consumer rm {info.stream_name} {durable}"
        )

    # Compatibility aliases
    _safe_ack = safe_ack
    _safe_nak = safe_nak
    _safe_term = safe_term
    _safe_in_progress = safe_in_progress
    _handle_jetstream_event = handle_jetstream_event
    make_jetstream_event_callback = make_event_callback
