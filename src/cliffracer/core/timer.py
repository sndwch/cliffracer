"""Interval-based timer scheduling for service methods."""

import asyncio
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from .correlation import CorrelationContext
from .decorators import refuse_bare_use
from .extension import WorkerContext


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
        token_factory: Callable[[], str] | None = None,
    ):
        """
        Initialize timer configuration.

        Args:
            interval: Time in seconds between executions
            eager: If True, execute immediately on service start
            max_drift: Maximum drift tolerance in seconds
            error_backoff: Delay after error before retry
            headers: Optional headers passed in WorkerContext
            token_factory: Optional callable returning a bearer token
        """
        self.interval = interval
        self.eager = eager
        self.max_drift = max_drift
        self.error_backoff = error_backoff
        self.headers = headers
        self.token_factory = token_factory

        self.method_name: str | None = None
        self.service_instance: Any | None = None
        self.task: asyncio.Task | None = None
        self.is_running = False
        self._stop_event: asyncio.Event = asyncio.Event()

        # Statistics
        self.execution_count = 0
        self.error_count = 0
        self.last_execution_time = 0.0
        self.total_execution_time = 0.0

    def clone(self) -> "Timer":
        """Create an independent copy of this Timer with the same configuration."""
        c = Timer(
            interval=self.interval,
            eager=self.eager,
            max_drift=self.max_drift,
            error_backoff=self.error_backoff,
            headers=dict(self.headers) if self.headers else None,
            token_factory=self.token_factory,
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

    async def start(self, service_instance: Any) -> None:
        """Start the timer task"""
        if self.is_running:
            logger.warning(f"Timer {self.method_name} already running")
            return

        self.service_instance = service_instance
        self.is_running = True
        self._stop_event = asyncio.Event()

        # Create and start the timer task
        self.task = asyncio.create_task(self._timer_loop())

        logger.info(
            f"Started timer {self.method_name} with {self._schedule_description}"
            f"{' (eager)' if self.eager else ''}"
        )

    @property
    def _schedule_description(self) -> str:
        """Human-readable schedule, overridable by subclasses (e.g. CronTimer)."""
        return f"{self.interval}s interval"

    async def stop(self) -> None:
        """Stop the timer task"""
        if not self.is_running:
            return

        self.is_running = False
        self._stop_event.set()

        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

        logger.info(f"Stopped timer {self.method_name}")

    async def _timer_loop(self) -> None:
        """Main timer execution loop"""
        next_execution = time.time()

        # Handle eager execution
        if self.eager:
            await self._execute_method()

        # Set up the next execution time
        next_execution += self.interval

        while self.is_running:
            try:
                current_time = time.time()
                sleep_time = next_execution - current_time

                # Check for drift
                if sleep_time < -self.max_drift:
                    logger.warning(
                        f"Timer {self.method_name} drifted by {-sleep_time:.2f}s, "
                        "adjusting schedule"
                    )
                    next_execution = current_time + self.interval
                    sleep_time = self.interval

                # Wait for next execution or stop signal
                if sleep_time > 0:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_time)
                        # Stop event was set
                        break
                    except TimeoutError:
                        # Timeout reached, time to execute
                        pass

                if not getattr(self, "is_running", True):
                    break

                # Execute the method
                await self._execute_method()

                # Schedule next execution
                next_execution += self.interval

            except Exception as e:
                logger.error(f"Error in timer loop for {self.method_name}: {e}")
                self.error_count += 1

                # Apply error backoff
                await asyncio.sleep(self.error_backoff)
                next_execution = time.time() + self.interval

    async def _execute_method(self) -> None:
        """Execute the timer method with error handling and metrics"""
        if not self.service_instance or not self.method_name:
            return

        method = getattr(self.service_instance, self.method_name, None)
        if not method:
            logger.error(f"Timer method {self.method_name} not found")
            return

        execution_start = time.time()
        self.execution_count += 1

        # Each firing is a discrete root operation. Clear ambient correlation
        # context before and after execution so identifiers do not leak across
        # periodic task runs.
        CorrelationContext.clear()
        try:
            logger.debug(f"Executing timer method {self.method_name}")

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
                    if tok:
                        headers["authorization"] = (
                            tok if tok.lower().startswith("bearer ") else f"Bearer {tok}"
                        )
                await runner(
                    WorkerContext(
                        kind="timer",
                        subject=None,
                        headers=headers,
                        correlation_id=None,
                        payload={},
                    ),
                    _call,
                )
            else:
                await _call()

            execution_time = time.time() - execution_start
            self.last_execution_time = execution_time
            self.total_execution_time += execution_time

            logger.debug(f"Timer method {self.method_name} completed in {execution_time:.3f}s")

            # Record metrics if available
            if hasattr(self.service_instance, "_metrics") and self.service_instance._metrics:
                self.service_instance._metrics.increment_counter(
                    f"timer_{self.method_name}_executions"
                )
                self.service_instance._metrics.record_custom_metric(
                    f"timer_{self.method_name}_duration_ms", execution_time * 1000
                )

        except Exception as e:
            execution_time = time.time() - execution_start
            self.error_count += 1

            logger.error(f"Error executing timer method {self.method_name}: {e}", exc_info=True)

            # Record error metrics if available
            if hasattr(self.service_instance, "_metrics") and self.service_instance._metrics:
                self.service_instance._metrics.increment_counter(f"timer_{self.method_name}_errors")

        finally:
            # Clear correlation context so the completed firing does not outlive its execution.
            CorrelationContext.clear()

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
            "last_execution_time": self.last_execution_time,
            "average_execution_time": avg_execution_time,
            "total_execution_time": self.total_execution_time,
            "error_rate": (self.error_count / max(self.execution_count, 1)) * 100,
        }


def timer(
    interval: float,
    eager: bool = False,
    headers: dict[str, str] | None = None,
    token_factory: Callable[[], str] | None = None,
    **kwargs: Any,
) -> Callable[..., Any]:
    """
    Decorator for creating timer-triggered methods.

    The decorated method will be called every `interval` seconds.

    Args:
        interval: Time in seconds between executions
        eager: If True, execute immediately on service start
        headers: Optional headers passed in WorkerContext
        token_factory: Optional callable returning a bearer token
        **kwargs: Additional timer configuration options

    Example:
        @timer(interval=30)
        async def health_check(self):
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
