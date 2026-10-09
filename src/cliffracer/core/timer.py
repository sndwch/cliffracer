"""Interval-based timer scheduling for service methods."""

import asyncio
import contextvars
import inspect
from collections.abc import Awaitable, Callable
from math import isfinite
from typing import Any

from loguru import logger

from .clock import REAL_CLOCK, Clock
from .correlation import CorrelationContext
from .credentials import bearer
from .deadline import Deadline, scoped
from .decorators import refuse_bare_use
from .exceptions import ConfigurationError
from .extension import RejectMessage, WorkerContext

#: The run of a timer that the current context is inside: the timer and its run number. Set while a
#: handler runs and inherited by every task the handler (or the service's stop, if the handler
#: calls it) creates, which is how `Timer.stop` knows it has been called from the run it would
#: otherwise be waiting for.
_RUNNING: contextvars.ContextVar[tuple["Timer", int] | None] = contextvars.ContextVar(
    "cliffracer_timer_run", default=None
)

#: The header a timer firing carries its `token_factory` token in. An extension that authenticates
#: callers reads a timer firing's token from here as well as from the header it is configured for.
TIMER_TOKEN_HEADER = "authorization"


#: Seconds a `Timer.stop()` that has no hand-over waits for a run it cancelled, when the timer is
#: not attached to a service whose `shutdown_timeout` says otherwise. It is the default of
#: `ServiceConfig.shutdown_timeout`.
STANDALONE_CANCEL_GRACE = 30.0

#: The default of `Timer.stop(cancel_grace=)`: take the bound from the service the timer belongs to.
_FROM_THE_SERVICE: Any = object()


def _seconds(name: str, value: Any, *, minimum: float, exclusive: bool = False) -> None:
    """Refuse a timer option that is not a finite number of seconds at or above `minimum`.

    A bool is an `int` to `isinstance` and is refused. `exclusive` makes the bound strict.
    """
    ok = (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and isfinite(value)
        and (value > minimum if exclusive else value >= minimum)
    )
    if not ok:
        bound = f"greater than {minimum:g}" if exclusive else f"at least {minimum:g}"
        raise ConfigurationError(
            f"Timer {name} must be a finite number of seconds {bound}, got {value!r} "
            f"({type(value).__name__})"
        )


def _a_clock(clock: Any) -> Clock:
    """The clock a timer was given, or the real one; anything that is not a clock is refused."""
    if clock is None:
        return REAL_CLOCK
    if not isinstance(clock, Clock):
        raise ConfigurationError(
            f"Timer clock must have monotonic(), now(tz), sleep() and wait(), got {clock!r} "
            f"({type(clock).__name__})"
        )
    return clock


def _read_outcome(task: "asyncio.Task[Any]") -> None:
    """Retrieve a finished task's outcome so a late failure is not reported as never retrieved."""
    if not task.cancelled():
        task.exception()


class _DeadlineExceeded(Exception):
    """A firing cut off at its deadline; its text says how long it was given and ran."""


class Timer:
    """
    Timer entrypoint for periodic method execution.

    Fires every `interval` seconds or as soon as the previous execution
    completes if that took longer. The default behavior is to wait
    `interval` seconds before firing for the first time. If you want
    the timer to fire as soon as the service starts, pass `eager=True`.
    """

    def __init__(
        self,
        interval: float,
        eager: bool = False,
        max_drift: float = 1.0,
        error_backoff: float = 5.0,
        headers: dict[str, str] | None = None,
        token_factory: Callable[[], str | Awaitable[str]] | None = None,
        clock: Clock | None = None,
        deadline: float | None = None,
    ):
        """
        Initialize timer configuration.

        Args:
            interval: Time in seconds between executions
            eager: If True, execute immediately on service start
            max_drift: Maximum drift tolerance in seconds
            error_backoff: Delay after error before retry
            headers: Optional headers passed in WorkerContext
            token_factory: Optional callable returning a bearer token, or an awaitable of one
            clock: What the timer reads time and waits through; the real clock when omitted. A
                test passes `cliffracer.testing.FakeClock` to move time instead of waiting for it.
            deadline: Seconds each firing may run, hooks included, before it is cancelled and
                counted as an error. Calls the firing makes wait at most what is left of it. Unset
                is no deadline. It is on the event loop's clock, as a request's deadline is, not on
                `clock`.
        """
        self._check_interval(interval)
        if deadline is not None:
            _seconds("deadline", deadline, minimum=0, exclusive=True)
        _seconds("max_drift", max_drift, minimum=0)
        _seconds("error_backoff", error_backoff, minimum=0)
        self.interval = interval
        self.eager = eager
        self.max_drift = max_drift
        self.error_backoff = error_backoff
        self.headers = headers
        self.token_factory = token_factory
        self.clock = _a_clock(clock)
        self.deadline = deadline

        self.method_name: str | None = None
        self.service_instance: Any | None = None
        self.task: asyncio.Task | None = None
        self.is_running = False
        self._executing = False
        self._run = 0
        self._stop_event: asyncio.Event = asyncio.Event()

        # Statistics
        self.execution_count = 0
        self.error_count = 0
        # Firings an extension turned away. A refusal is the firing being refused, not the service
        # being broken, so it is counted here and not in `error_count`.
        self.refusal_count = 0
        # The latest firing's failure as "Type: message", or None when it succeeded or was refused.
        self.last_error: str | None = None
        # The class name of that failure, which names what failed without carrying its text.
        self.last_error_type: str | None = None
        # The latest firing's refusal reason, or None when it was not refused.
        self.last_refusal: str | None = None
        self.last_execution_time = 0.0
        self.total_execution_time = 0.0

    def _check_interval(self, interval: Any) -> None:
        """Refuse an interval the loop cannot wait for: zero, negative, non-finite or not a number."""
        _seconds("interval", interval, minimum=0, exclusive=True)

    def clone(self) -> "Timer":
        """Create an independent copy of this Timer with the same configuration."""
        c = Timer(
            interval=self.interval,
            eager=self.eager,
            max_drift=self.max_drift,
            error_backoff=self.error_backoff,
            headers=dict(self.headers) if self.headers else None,
            token_factory=self.token_factory,
            clock=self.clock,
            deadline=self.deadline,
        )
        c.method_name = self.method_name
        return c

    def __call__(self, method: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator to mark method as timer-triggered"""
        self.method_name = method.__name__

        # Add timer metadata to method
        m: Any = method
        if not hasattr(m, "_cliffracer_timers"):
            m._cliffracer_timers = []
        m._cliffracer_timers.append(self)

        return method

    @property
    def _log(self) -> Any:
        """The loguru logger bound to the service this timer belongs to, once it has started."""
        name = getattr(getattr(self.service_instance, "config", None), "name", None)
        return logger.bind(service=name) if name else logger

    async def start(self, service_instance: Any) -> None:
        """Start the timer task"""
        if self.is_running:
            self._log.warning(f"Timer {self.method_name} already running")
            return

        self.service_instance = service_instance
        self.is_running = True
        self._stop_event = asyncio.Event()

        # Create and start the timer task
        self.task = asyncio.create_task(self._timer_loop(), name=f"timer:{self.method_name}")

        self._log.info(
            f"Started timer {self.method_name} with {self._schedule_description}"
            f"{' (eager)' if self.eager else ''}"
        )

    @property
    def _schedule_description(self) -> str:
        """Human-readable schedule, overridable by subclasses (e.g. CronTimer)."""
        return f"{self.interval}s interval"

    async def stop(
        self,
        grace: float | None = 0.0,
        *,
        hand_over: Callable[[asyncio.Task[Any]], None] | None = None,
        cancel_grace: float | None = _FROM_THE_SERVICE,
    ) -> None:
        """Stop the timer task, letting a run already under way finish within `grace` seconds.

        The timer stops scheduling at once. A run that is in flight is given `grace` seconds to
        finish on its own before it is cancelled: `0` (the default) cancels it immediately, and
        `None` waits for it however long it takes. A timer that is only waiting for its next
        firing has nothing in flight and is cancelled at once whatever the grace.

        A cancelled run is then given `cancel_grace` seconds to finish its cleanup, unless
        `hand_over` is given: the cancelled task that has not finished is passed to it and
        `stop()` returns, so that whoever holds the budget for work that is slow to stop (the
        service's drain) bounds it and reports it. A run that catches `CancelledError` and carries
        on is such work. Without a hand-over, a run still going when `cancel_grace` ends is
        reported at error level and left running, and `stop()` returns. `cancel_grace` defaults to
        the `shutdown_timeout` of the service the timer belongs to, or `STANDALONE_CANCEL_GRACE`
        for a timer that belongs to none; `None` waits for the run however long it takes.

        A cancellation of `stop()` itself cancels the task too and then propagates.
        """
        if not self.is_running:
            return

        self.is_running = False
        self._stop_event.set()

        if self._is_inside_its_own_run():
            # Called from the handler this timer is running, as a watchdog or a one-shot job that
            # stops its own service does, through however many tasks the stop spreads over.
            # Waiting for the run would wait for the caller, and cancelling it would cancel the
            # stop that is cancelling it. The loop ends when the handler returns, because the
            # timer is no longer running.
            self._log.info(f"Stopping timer {self.method_name} from its own handler")
            return

        task = self.task
        if task and not task.done():
            try:
                if self._executing and (grace is None or grace > 0):
                    await asyncio.wait({task}, timeout=grace)
                if not task.done():
                    task.cancel()
                    if hand_over is None:
                        bound = self._cancel_grace(cancel_grace)
                        await asyncio.wait({task}, timeout=bound)
                        if not task.done():
                            self._log.error(
                                f"Timer {self.method_name} did not stop within {bound:g}s of "
                                f"cancellation; the run is still going and stop() is returning"
                            )
                            task.add_done_callback(_read_outcome)
                    elif not task.done():
                        hand_over(task)
            except asyncio.CancelledError:
                task.cancel()
                if hand_over is not None and not task.done():
                    hand_over(task)
                raise
            if task.done() and not task.cancelled():
                # Read the outcome so a failure is not reported as never retrieved.
                task.exception()

        self._log.info(f"Stopped timer {self.method_name}")

    def _cancel_grace(self, requested: float | None) -> float | None:
        """The seconds a stop with no hand-over waits for a cancelled run (`None`: without bound)."""
        if requested is not _FROM_THE_SERVICE:
            return requested
        config = getattr(self.service_instance, "config", None)
        if config is not None and hasattr(config, "shutdown_timeout"):
            from .lifecycle import bounded_shutdown_timeout

            configured = bounded_shutdown_timeout(
                config.shutdown_timeout, self._log, f"Timer {self.method_name}"
            )
            return None if configured is None else float(configured)
        return STANDALONE_CANCEL_GRACE

    def _is_inside_its_own_run(self) -> bool:
        """Whether the caller is the run of this timer that is in flight now.

        A task a finished run left behind carries that run's number, which is no longer the
        current one, so it is not mistaken for the run in flight.
        """
        current = _RUNNING.get()
        return (
            self._executing
            and current is not None
            and current[0] is self
            and current[1] == self._run
        )

    async def _timer_loop(self) -> None:
        """Main timer execution loop"""
        clock = self.clock
        next_execution = clock.monotonic()

        # Handle eager execution. An error in it is handled as a scheduled
        # firing's is below: logged, counted, backed off, and the schedule rebased.
        if self.eager:
            try:
                await self._execute_method()
            except Exception as e:
                self._log.error(f"Error in timer loop for {self.method_name}: {e}")
                self.error_count += 1
                await self._back_off()
                next_execution = clock.monotonic()

        # Set up the next execution time
        next_execution += self.interval

        while self.is_running:
            try:
                current_time = clock.monotonic()
                sleep_time = next_execution - current_time

                # Check for drift
                if sleep_time < -self.max_drift:
                    self._log.warning(
                        f"Timer {self.method_name} drifted by {-sleep_time:.2f}s, "
                        "adjusting schedule"
                    )
                    next_execution = current_time + self.interval
                    sleep_time = self.interval

                # Wait for next execution or stop signal
                if sleep_time > 0 and await clock.wait(self._stop_event, sleep_time):
                    # Stop event was set
                    break

                if not getattr(self, "is_running", True):
                    break

                # Execute the method
                await self._execute_method()

                # Schedule next execution
                next_execution += self.interval

            except Exception as e:
                self._log.error(f"Error in timer loop for {self.method_name}: {e}")
                self.error_count += 1

                # Apply error backoff
                await self._back_off()
                next_execution = clock.monotonic() + self.interval

    async def _back_off(self) -> None:
        """Wait `error_backoff` after a failed firing, or until the timer is stopped.

        A stop ends the backoff as it ends the wait for the next firing, and the loop then ends
        because the timer is no longer running.
        """
        await self.clock.wait(self._stop_event, self.error_backoff)

    async def _execute_method(self) -> None:
        """Execute the timer method with error handling and metrics"""
        self.last_error = None
        self.last_error_type = None
        self.last_refusal = None
        if not self.service_instance or not self.method_name:
            return
        self._executing = True

        method = getattr(self.service_instance, self.method_name, None)
        if not method:
            self._log.error(f"Timer method {self.method_name} not found")
            return

        execution_start = self.clock.monotonic()
        self.execution_count += 1

        # Each firing is a discrete root operation. Clear ambient correlation
        # context before and after execution so identifiers do not leak across
        # periodic task runs.
        CorrelationContext.clear()
        self._run += 1
        running = _RUNNING.set((self, self._run))
        try:
            self._log.debug(f"Executing timer method {self.method_name}")

            # Execute the method (handle both sync and async), inside the
            # service's hook chain so a timer firing is a dispatch like any
            # other. Guarded: a timer may be driven by a
            # service double that has no container.
            async def _call() -> Any:
                if asyncio.iscoroutinefunction(method):
                    return await method()
                return method()

            container = getattr(self.service_instance, "container", None)
            dispatcher = getattr(container, "dispatcher", None)
            runner = (
                getattr(self.service_instance, "_run_worker", None)
                or getattr(container, "_run_worker", None)
                or getattr(dispatcher, "_run_worker", None)
                or getattr(dispatcher, "run_worker", None)
            )
            if runner is not None:
                headers = dict(self.headers) if self.headers else {}
                if self.token_factory:
                    tok = self.token_factory()
                    if inspect.isawaitable(tok):
                        tok = await tok
                    if tok:
                        headers[TIMER_TOKEN_HEADER] = bearer(tok)
                ctx = WorkerContext(
                    kind="timer",
                    subject=None,
                    headers=headers,
                    correlation_id=None,
                    payload={},
                )
                # A timer has no subject; its method name is how a hook says which one fired.
                ctx.data["handler_name"] = self.method_name
                cut_off = await self._within_deadline(lambda: runner(ctx, _call))
            else:
                cut_off = await self._within_deadline(_call)
            if cut_off is not None:
                raise _DeadlineExceeded(cut_off)

            execution_time = self.clock.monotonic() - execution_start
            self.last_execution_time = execution_time
            self.total_execution_time += execution_time

            self._log.debug(f"Timer method {self.method_name} completed in {execution_time:.3f}s")

        except _DeadlineExceeded as cut_off:
            self.last_error = str(cut_off)
            self.last_error_type = "DeadlineExceeded"
            self.error_count += 1
            self._log.error("Timer method {} {}", self.method_name, cut_off)

        except RejectMessage as refusal:
            if refusal.hook_crash:
                # A gate that crashed is not a gate that refused: the service is broken, not the
                # firing turned away, so it is the error it is (an auth backend that is down makes
                # every firing one), counted and logged as one.
                self._record_a_fault(refusal)
            else:
                # A hook turned the firing away: the method did not run, and nothing is broken. It
                # is reported at WARNING, counted apart from errors, and does not count as an
                # execution, so neither the error rate nor the average duration moves.
                self.last_refusal = str(refusal)
                self.refusal_count += 1
                self.execution_count -= 1
                self._log.warning("Timer method {} refused: {}", self.method_name, refusal)

        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self.last_error_type = type(e).__name__
            self.error_count += 1

            self._log.opt(exception=True).error(
                "Error executing timer method {}: {}", self.method_name, e
            )

        finally:
            self._executing = False
            _RUNNING.reset(running)
            # Clear correlation context so the completed firing does not outlive its execution.
            CorrelationContext.clear()

    async def _within_deadline(self, run: Callable[[], Awaitable[Any]]) -> str | None:
        """Run the firing under its deadline, if it has one; what happened, when it was cut off.

        The deadline is the current one while the firing runs, so a call it makes waits at most
        what is left. A firing that suppresses the cancellation and returns is still cut off.
        """
        if self.deadline is None:
            await run()
            return None
        from .dispatch.rpc_limits import deadline_text, warn_if_still_running

        deadline = Deadline(
            asyncio.get_running_loop().time() + self.deadline, self.deadline, "timer"
        )
        name = self.method_name or "timer"
        bound = asyncio.timeout_at(deadline.at)
        still_running = warn_if_still_running(self._log, name, deadline)
        try:
            with scoped(deadline):
                async with bound:
                    await run()
        except TimeoutError:
            if not bound.expired():
                raise
        finally:
            if still_running is not None:
                still_running.cancel()
        if bound.expired():
            return deadline_text(name, deadline, ran=True)
        return None

    def _record_a_fault(self, fault: Exception) -> None:
        """Count and log a firing that failed because a gate crashed, as a failing method is."""
        self.last_error = f"{type(fault).__name__}: {fault}"
        self.last_error_type = type(fault).__name__
        self.error_count += 1
        self._log.opt(exception=True).error(
            "Error executing timer method {}: {}", self.method_name, fault
        )

    def get_stats(self) -> dict[str, Any]:
        """Get timer execution statistics"""
        avg_execution_time = (
            self.total_execution_time / self.execution_count if self.execution_count > 0 else 0.0
        )

        return {
            "method_name": self.method_name,
            "interval": self.interval,
            "eager": self.eager,
            "is_running": self.is_running,
            "execution_count": self.execution_count,
            "error_count": self.error_count,
            "refusal_count": self.refusal_count,
            "last_execution_time": self.last_execution_time,
            "average_execution_time": avg_execution_time,
            "total_execution_time": self.total_execution_time,
            "error_rate": (self.error_count / max(self.execution_count, 1)) * 100,
        }


def timer(
    interval: float,
    eager: bool = False,
    headers: dict[str, str] | None = None,
    token_factory: Callable[[], str | Awaitable[str]] | None = None,
    **kwargs: Any,
) -> Callable[..., Any]:
    """
    Decorator for creating timer-triggered methods.

    The decorated method will be called every `interval` seconds.

    Args:
        interval: Time in seconds between executions
        eager: If True, execute immediately on service start
        headers: Optional headers passed in WorkerContext
        token_factory: Optional callable returning a bearer token, or an awaitable of one
        **kwargs: Additional timer configuration options

    Example:
        @timer(interval=30)
        async def check_database(self):
            await self.check_database_connection()

        @timer(interval=60, eager=True)
        async def cleanup_cache(self):
            await self.remove_expired_entries()
    """

    refuse_bare_use(interval, "timer", "@timer(interval=60)")

    def decorator(method: Callable[..., Any]) -> Callable[..., Any]:
        timer_instance = Timer(
            interval=interval,
            eager=eager,
            headers=headers,
            token_factory=token_factory,
            **kwargs,
        )
        return timer_instance(method)

    return decorator
