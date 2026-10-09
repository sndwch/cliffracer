"""JetStream message handling, transport protections, and pull consumers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from loguru import logger as global_logger

from ..error_text import own_text
from ..extension import RetryMessage, is_a_finite_delay
from ..jetstream import (
    consumer_config_drift,
    consumer_config_for,
    nak_delay,
)
from ..service_config import ServiceConfig
from .dlq import DeadLetterPublisher, message_metadata
from .events import (
    DispatchOutcome,
    EventDispatcher,
    carried_correlation_id,
    carry_correlation_id,
)
from .handler_limits import not_started, release, take, would_wait


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


_REPORTED_ATTR = "_cliffracer_reported"


def _is_a_usable_delay(delay: Any) -> bool:
    """Whether a `RetryMessage.retry_after` can be sent as a NAK delay: a finite number above zero.

    Anything else, `None`, zero, a negative number, `nan` or `inf`, is no hint, and the NAK uses the
    configured backoff. `nan` and `inf` passed an `<= 0` test and then failed to encode in nats-py.
    """
    return is_a_finite_delay(delay) and delay > 0


def _already_reported(error: BaseException) -> BaseException:
    """Mark an error whose overrun was logged where it was raised, so the retry need not repeat it."""
    setattr(error, _REPORTED_ATTR, True)
    return error


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
        is_stopping: Callable[[], bool] | None = None,
    ) -> None:
        self.config = config
        self.connection_provider = connection_provider
        self.events = event_dispatcher
        self.dlq = dlq
        # Whether the service is stopping or stopped: a message that has waited for a permit is
        # not started then, see `_bounded_handle_jetstream_event`.
        self._is_stopping = is_stopping or (lambda: False)
        self.task_spawner = task_spawner
        self.logger = logger or global_logger.bind(service=config.name)
        self.service = service
        # The `max_deliver` each durable's server consumer enforces, by the
        # pattern it is subscribed under, as read at subscribe. See
        # `delivery_limit`.
        self._server_max_deliver: dict[str, int] = {}
        # The `ack_wait` the server enforces for each durable, by the same key, as read at
        # subscribe: see `_pulse_interval`.
        self._server_ack_wait: dict[str, float] = {}

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
        if not (hasattr(msg, "nak") and callable(msg.nak)):
            return False
        try:
            await msg.nak(delay=delay)
            return True
        except Exception as exc:
            self.logger.warning(
                f"Failed to NAK JetStream message on '{getattr(msg, 'subject', '')}' with "
                f"delay={delay:g}s: {exc}"
            )
        # The delayed form can fail where the plain one cannot, on a delay nats-py cannot encode:
        # without it the message would sit until `ack_wait`, and a plain NAK redelivers it now.
        # With no delay the two forms are one call, and a failure of it is not about the delay.
        if not delay:
            return False
        try:
            await msg.nak()
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
            if sem is not None or self.events.limit_of(pattern) is not None:
                # Spawned before the permit is taken, so this callback returns at once and the
                # server's `jetstream_max_ack_pending` bounds how many messages are held. A
                # callback that waited for the permit would hold every later message in the
                # client's queue, where nothing can tell the server they are still wanted.
                self._spawn_task(
                    self._bounded_handle_jetstream_event(msg, pattern),
                    name=f"jetstream_event_bounded:{pattern}",
                )
            else:
                self._spawn_task(
                    self.handle_jetstream_event(msg, pattern=pattern),
                    name=f"jetstream_event:{pattern}",
                )

        return _cb

    def _pulse_interval(self, pattern: str | None = None) -> float:
        """Seconds between in-progress pulses, half the `ack_wait` the server enforces.

        A durable keeps the `ack_wait` it was created with, so a service whose config asks for more
        than the server holds would pulse too slowly for it, and the server would redeliver the
        message under a running handler: the shorter of the two is the one that is paced against,
        as `delivery_limit` takes the lower `max_deliver`.
        """
        ack_wait = self.config.jetstream_ack_wait
        server = self._server_ack_wait.get(pattern) if pattern is not None else None
        if server is not None:
            ack_wait = min(ack_wait, server)
        # `ServiceConfig` pins `jetstream_ack_wait` above zero, so the heartbeat always runs.
        return max(0.05, ack_wait / 2.0)

    async def _bounded_handle_jetstream_event(self, msg: Any, pattern: str | None = None) -> None:
        """Take the handler's permit and the service's, pulsing the message while it waits; dispatch.

        The server's `ack_wait` runs from delivery, not from the handler's start. A message
        that waits here for longer than that is redelivered to a replica that still holds it,
        and the handler runs it again, so the heartbeat starts when the message is received
        rather than when the handler starts. A message that gets its permit while the service
        is stopping is not started: it is left for the broker to redeliver.
        """
        sem, held = self.events.get_event_semaphore(), self.events.limit_of(pattern)
        if sem is None and held is None:
            await self._dispatch(msg, pattern)
            return
        acquired = False
        try:
            if would_wait(held, sem):
                async with _JetStreamHeartbeat(self, msg, self._pulse_interval(pattern)):
                    acquired = await take(held, sem, None)
                # The handler's own heartbeat pulses one interval after it starts; this keeps
                # the gap from the last pulse made while waiting to one interval.
                await self.safe_in_progress(msg)
            else:
                acquired = await take(held, sem, None)
            if self._is_stopping():
                not_started(held)
                # A stopping service finishes the work it has running and starts no more. This
                # message is neither acknowledged nor terminated, so the broker redelivers it
                # when its `ack_wait` passes, to this service after a restart or to a replica.
                self.logger.info(
                    f"Not starting a JetStream message on '{getattr(msg, 'subject', '')}' "
                    f"that waited for a permit: the service is stopping, so the broker "
                    f"redelivers it"
                )
                return
            await self._dispatch(msg, pattern)
        finally:
            if acquired:
                release(held, sem)

    async def _dispatch(self, msg: Any, pattern: str | None) -> None:
        if pattern is not None:
            await self.handle_jetstream_event(msg, pattern=pattern)
        else:
            await self.handle_jetstream_event(msg)

    def _handler_name(self, pattern: str | None, subject: str) -> str:
        """The handler function's name, for a log line an operator has to act on.

        A subject alone does not find the code when the listener is registered
        against a wildcard. Falls back to the pattern, then to the subject, so
        this never becomes the reason a warning cannot be emitted.
        """
        handlers = getattr(getattr(self.events, "registry", None), "event_handlers", {})
        handler = handlers.get(pattern) if pattern else None
        if handler is None:
            handler = handlers.get(subject)
        name = getattr(handler, "__name__", None)
        return f"handler {name}" if name else f"the handler for {pattern or subject}"

    async def _handle_within_budget(
        self, msg: Any, pattern: str | None, budget: float | None
    ) -> Any:
        """Dispatch, cancelling the handler if it outlives `budget`.

        The heartbeat keeps a SLOW handler from being redelivered under itself,
        which is what it is for. A WEDGED one it keeps alive forever: the ack
        timer is reset for as long as the handler has not returned, so
        `num_delivered` never increments, so the `max_deliver` branch is never
        reached and nothing is ever dead-lettered. The safety net is disabled by
        exactly the failure it exists to catch.

        The deadline cancels the handler when the budget is spent, which is the
        half that stops a wedged replica accumulating one leaked task per
        message. Cancelling mid-flight is acceptable on this path: a JetStream
        handler already lives under redelivery, so partial work followed by a
        retry is the contract it signed, and one that cannot tolerate
        cancellation cannot tolerate redelivery either.

        Whether the budget fired is read from the deadline, never from the
        exception type. A handler that suppresses the cancellation and returns
        completes the await normally, and would otherwise be acked; a handler's
        own `TimeoutError` is the same builtin the deadline raises, and would
        otherwise be reported as an overrun. A handler that suppresses the
        cancellation and keeps running cannot be dispositioned without racing
        its own redelivery, so it is only warned about, once it has run a
        second budget past the deadline.

        The timeout is raised as a plain `TimeoutError` rather than a type of
        its own because the dead-letter record keeps `str(error)` and not the
        class, so a new class would be invisible where it matters; the message
        carries the budget and the elapsed time instead.
        """
        # The id the dispatch ran under, which a handler that is cancelled cannot hand over on an
        # exception: the overrun is raised here, as a new error, and carries it from this list.
        ran_under: list[str] = []
        run = self.events.handle_event(
            msg, pattern=pattern, raise_on_error=True, ran_under=ran_under
        )
        if budget is None:
            return await run

        subject = getattr(msg, "subject", "<unknown>")
        # The handler's own name as well as the subject: an operator choosing
        # a number needs to find the code, and a wildcard pattern does not name
        # one function.
        handler = self._handler_name(pattern, subject)
        started = time.monotonic()
        still_running = asyncio.get_running_loop().call_later(
            2 * budget,
            lambda: self.logger.warning(
                f"{handler} is still running {time.monotonic() - started:.3f}s after "
                f"being cancelled at max_processing_time ({budget}s) on {subject}: it "
                f"is suppressing the cancellation, so the message stays in flight "
                f"with no disposition"
            ),
        )
        deadline = asyncio.timeout(budget)
        try:
            async with deadline:
                result = await run
        except TimeoutError as exc:
            if not deadline.expired():
                raise
            elapsed = time.monotonic() - started
            self.logger.warning(
                f"{handler} exceeded max_processing_time ({budget}s) on {subject}; "
                f"cancelled after {elapsed:.3f}s and the message will be redelivered"
            )
            overrun = TimeoutError(
                f"handler for {subject} exceeded max_processing_time of "
                f"{budget}s and was cancelled after {elapsed:.3f}s"
            )
            carry_correlation_id(overrun, ran_under[0] if ran_under else None)
            raise _already_reported(own_text(overrun)) from exc
        finally:
            still_running.cancel()

        if deadline.expired():
            elapsed = time.monotonic() - started
            self.logger.warning(
                f"{handler} exceeded max_processing_time ({budget}s) on {subject} and "
                f"suppressed the cancellation, returning after {elapsed:.3f}s; the "
                f"message will be redelivered"
            )
            overrun = TimeoutError(
                f"handler for {subject} exceeded max_processing_time of {budget}s and "
                f"suppressed the cancellation, returning after {elapsed:.3f}s"
            )
            carry_correlation_id(overrun, ran_under[0] if ran_under else None)
            raise _already_reported(own_text(overrun))
        return result

    def delivery_limit(self, pattern: str | None) -> tuple[int, str]:
        """The delivery on which a failing message is dead-lettered, and whose limit it is.

        `min(server, config)`. A durable keeps the `max_deliver` it was created
        with, so a service whose config asks for more than the server enforces
        would NAK the server's last delivery: the server stops redelivering, and
        the message is never dead-lettered or logged. The server's number is
        read once per durable at subscribe, so this costs no round-trip.

        The config decides when it is the lower, when the server is unlimited,
        and when nothing was read -- no durable, an unreadable consumer, or a
        dispatch with no subscription behind it.

        The second value names the deciding limit for the dead-letter record,
        so an operator is sent to the right config.
        """
        config_limit = self.config.jetstream_max_deliver
        server_limit = self._server_max_deliver.get(pattern) if pattern is not None else None
        if server_limit is not None and 0 < server_limit < config_limit:
            return server_limit, f"server max_deliver {server_limit}"
        return config_limit, f"config jetstream_max_deliver {config_limit}"

    async def _dead_letter_and_terminate(
        self, msg: Any, error: Exception, num_delivered: int, decided_by: str
    ) -> None:
        """Dead-letter a delivery that reached its limit, then terminate it whatever that did.

        The dead letter is best-effort and reports a failure by returning False. Should it raise
        instead, the failure is logged and counted in `dead_letters_lost`, and the terminate still
        goes out: a delivery left with no ack, nak or term is redelivered until the server stops,
        and is never dead-lettered or counted.
        """
        try:
            await self.dlq.dead_letter_terminated(
                msg,
                error,
                num_delivered,
                delivery_limit=decided_by,
                correlation_id=carried_correlation_id(error),
            )
        except Exception as dead_letter_error:
            self.dlq.lost += 1
            self.logger.error(
                f"Could not dead-letter '{getattr(msg, 'subject', '<unknown>')}' "
                f"({type(dead_letter_error).__name__}: {dead_letter_error}). Terminating anyway."
            )
        await self.safe_term(msg)

    async def handle_jetstream_event(self, msg: Any, *, pattern: str | None = None) -> None:
        """JetStream event dispatch with pulse heartbeat, ack, nak, or termination."""
        pulse_interval = self._pulse_interval(pattern)

        budget = getattr(self.config, "max_processing_time", None)

        try:
            async with _JetStreamHeartbeat(self, msg, pulse_interval):
                outcome = await self._handle_within_budget(msg, pattern, budget)
        except RetryMessage as error:
            num_delivered = getattr(message_metadata(msg), "num_delivered", 1)
            limit, decided_by = self.delivery_limit(pattern)
            if num_delivered >= limit:
                await self._dead_letter_and_terminate(msg, error, num_delivered, decided_by)
            else:
                delay = error.retry_after if _is_a_usable_delay(error.retry_after) else None
                if delay is None:
                    delay = nak_delay(num_delivered, self.config)
                self.logger.warning(
                    f"{self._handler_name(pattern, getattr(msg, 'subject', '<unknown>'))} deferred "
                    f"{getattr(msg, 'subject', '<unknown>')} (delivery {num_delivered}/{limit}): "
                    f"{error}; NAKing with delay={delay:g}s"
                )
                await self.safe_nak(msg, delay=delay)
            return
        except Exception as error:
            num_delivered = getattr(message_metadata(msg), "num_delivered", 1)
            limit, decided_by = self.delivery_limit(pattern)
            if num_delivered >= limit:
                await self._dead_letter_and_terminate(msg, error, num_delivered, decided_by)
            else:
                delay = nak_delay(num_delivered, self.config)
                # The retry is the one outcome with no other trace: the handler's
                # exception was re-raised unlogged to be classified here, so this
                # line is the only evidence of a redelivery storm until the
                # dead-letter record at the limit.
                if not getattr(error, _REPORTED_ATTR, False):
                    self.logger.warning(
                        f"{self._handler_name(pattern, getattr(msg, 'subject', '<unknown>'))} "
                        f"failed on {getattr(msg, 'subject', '<unknown>')} (delivery "
                        f"{num_delivered}/{limit}, {decided_by}): "
                        f"{type(error).__name__}: {error}; NAKing with delay={delay:g}s"
                    )
                await self.safe_nak(msg, delay=delay)
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
        except TimeoutError:  # nats' own TimeoutError is a subclass of the builtin
            return 0

        # The batch is dispatched CONCURRENTLY, bounded by the same semaphore the
        # push path uses. Awaiting each task before starting the next made the
        # batch serial: a fetch of 8 held one message in flight, so
        # `max_event_concurrency` never applied on this path and a bigger
        # `jetstream_max_ack_pending` bought nothing.
        #
        # Every message is spawned at once and takes its permit inside its own task, which
        # pulses it while it waits: the fetch delivered the whole batch, so the server's
        # `ack_wait` is running for all of it, not only for the message the loop has reached.
        limited = (
            self.events.get_event_semaphore() is not None
            or self.events.limit_of(pattern) is not None
        )
        tasks: list[asyncio.Task[Any]] = []
        for msg in msgs:
            if limited:
                coro = self._bounded_handle_jetstream_event(msg, pattern)
            elif pattern is not None:
                coro = self.handle_jetstream_event(msg, pattern=pattern)
            else:
                coro = self.handle_jetstream_event(msg)
            tasks.append(self._spawn_task(coro, name="jetstream_pull_event"))

        # Shielded, which is what the per-message `shield` was doing and is worth
        # keeping: `cancel_subscriptions` cancels `pull_loop` as step 3 of
        # shutdown and `drain_active_tasks` waits for handlers as step 4, so a
        # cancellation here must not reach them. Every task above is supervised,
        # so the drain is what finishes them.
        if tasks:
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        return len(msgs)

    def fetch_retry_delay(self, failures: int) -> float:
        """Seconds to wait after the `failures`-th fetch in a row that raised.

        The base is `jetstream_nak_backoff`, or a second where that is lower, so a second at least:
        `jetstream_nak_backoff` paces redelivery of a NAKed message and may be 0. The delay doubles
        to `jetstream_max_backoff` (or a second, where that is lower). A fetch that fails is a
        consumer deleted on the server, a subscription that is no longer valid or a permission
        error, which a retry a moment later does not cure: at a delay of 0 the loop was a spin that
        logged an error on every pass.
        """
        base = max(self.config.jetstream_nak_backoff, 1.0)
        ceiling = max(self.config.jetstream_max_backoff, 1.0)
        return float(min(base * 2 ** min(failures - 1, 63), ceiling))

    async def pull_loop(
        self,
        sub: Any,
        durable: str,
        *,
        pattern: str | None = None,
        is_running_fn: Callable[[], bool] | None = None,
        unsubscribe: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Continually fetch batches from a JetStream pull consumer while running."""
        failures = 0
        try:
            while is_running_fn() if is_running_fn else True:
                try:
                    count = await self.pull_once(sub, pattern=pattern)
                    failures = 0
                    if count == 0:
                        await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    delay = self.fetch_retry_delay(failures)
                    self.logger.error(
                        f"pull loop for durable {durable!r} failed: {exc}; fetching again in "
                        f"{delay:g}s"
                    )
                    await asyncio.sleep(delay)
        finally:
            if (
                self.nc
                and not getattr(self.nc, "is_draining", False)
                and not getattr(self.nc, "is_closed", False)
            ):
                try:
                    await (unsubscribe() if unsubscribe is not None else sub.unsubscribe())
                except Exception as exc:
                    # The connection is up (checked above), so this is not the expected
                    # shutdown failure: the durable keeps its binding.
                    self.logger.warning(
                        f"could not unsubscribe the pull consumer for durable {durable!r} "
                        f"on loop exit: {type(exc).__name__}: {exc}"
                    )

    async def report_consumer_drift(
        self, sub: Any, durable: str, *, pattern: str | None = None
    ) -> None:
        """Warn when server consumer configuration diverges from service configuration.

        Also records the server's `max_deliver` and `ack_wait` under `pattern`:
        `delivery_limit` reads the first, so the dead-letter decision is taken
        against the number the server enforces, and `_pulse_interval` the second,
        so the heartbeat is paced against the time the server allows. This is the only read of the consumer, and
        it never stops a service starting.
        """
        log = getattr(self.service, "logger", self.logger)
        if pattern is not None:
            # Dropped before the read, so a failed re-read cannot leave a
            # reading from a durable that may no longer exist.
            self._server_max_deliver.pop(pattern, None)
            self._server_ack_wait.pop(pattern, None)
        try:
            info = await sub.consumer_info()
            drift = consumer_config_drift(consumer_config_for(self.config), info.config)
        except Exception as exc:
            if pattern is None:
                log.warning(
                    f"could not read consumer info for durable {durable!r}: {exc}. Whether the "
                    f"server's tuning matches this service's configuration is not known."
                )
                return
            limit, decided_by = self.delivery_limit(pattern)
            log.warning(
                f"could not read consumer info for durable {durable!r}: {exc}. Failing "
                f"messages are dead-lettered after delivery {limit} ({decided_by}), "
                f"which the server may not be enforcing."
            )
            return

        server_limit = getattr(info.config, "max_deliver", None)
        if (
            pattern is not None
            and isinstance(server_limit, int)
            and not isinstance(server_limit, bool)
        ):
            self._server_max_deliver[pattern] = server_limit
        server_ack_wait = getattr(info.config, "ack_wait", None)
        if (
            pattern is not None
            and isinstance(server_ack_wait, int | float)
            and not isinstance(server_ack_wait, bool)
            and server_ack_wait > 0
        ):
            self._server_ack_wait[pattern] = float(server_ack_wait)

        if not drift:
            return

        fields = "; ".join(
            f"{name}={actual!r}, not the {want!r} asked for" for name, want, actual in drift
        )
        consequence = ""
        limit, decided_by = self.delivery_limit(pattern)
        if decided_by.startswith("server"):
            consequence = (
                f" Failing messages are dead-lettered after delivery {limit}, the server's limit."
            )
        log.warning(
            f"durable {durable!r} runs with {fields}. A durable's config is fixed "
            f"when it is created and a later subscribe does not update it.{consequence} To "
            f"apply the new tuning: nats consumer rm {info.stream_name} {durable}"
        )

    # Compatibility aliases
    _safe_ack = safe_ack
    _safe_nak = safe_nak
    _safe_term = safe_term
    _safe_in_progress = safe_in_progress
    _handle_jetstream_event = handle_jetstream_event
    make_jetstream_event_callback = make_event_callback
