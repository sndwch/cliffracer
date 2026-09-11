"""Cron-based timer scheduling for service methods.

Provides ``CronTimer`` and the ``@cron`` decorator for running service methods on
a cron schedule (e.g. "0 9 * * *" for 09:00 daily). Cron expressions are evaluated
in UTC by default; pass ``tz`` to evaluate in another timezone.

``CronTimer`` subclasses :class:`~cliffracer.core.timer.Timer` and reuses its
execution, stop, and discovery machinery — a ``@cron`` handler is registered under
the same ``_cliffracer_timers`` marker as ``@timer``, so the service start/stop
lifecycle needs no special handling.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from loguru import logger

from cliffracer.core.decorators import refuse_bare_use
from cliffracer.core.timer import Timer


class CronTimer(Timer):
    """A Timer that fires on a cron schedule instead of a fixed interval."""

    def __init__(
        self,
        expression: str,
        tz: str = "UTC",
        eager: bool = False,
        max_drift: float = 1.0,
        error_backoff: float = 5.0,
        headers: dict[str, str] | None = None,
        token_factory: Callable[[], str] | None = None,
    ):
        """
        Args:
            expression: A cron expression (e.g. "0 9 * * *") or named schedule
                (e.g. "@hourly"). Validated immediately.
            tz: IANA timezone name the expression is evaluated in (default "UTC").
            eager: If True, run the method once on service start, then follow the schedule.
            max_drift: Inherited from Timer (unused by the cron loop, kept for API parity).
            error_backoff: Delay in seconds after an error before resuming the schedule.
            headers: Optional headers passed in WorkerContext.
            token_factory: Optional callable returning a bearer token.

        Raises:
            ValueError: If the cron expression or timezone is invalid.
        """
        if not croniter.is_valid(expression):
            raise ValueError(f"Invalid cron expression: {expression!r}")
        try:
            self._tzinfo = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"Invalid timezone: {tz!r}") from e

        # interval is unused by the cron loop; pass 0 to satisfy the base initializer.
        super().__init__(
            interval=0,
            eager=eager,
            max_drift=max_drift,
            error_backoff=error_backoff,
            headers=headers,
            token_factory=token_factory,
        )
        self.expression = expression
        self.tz = tz

    def clone(self) -> "CronTimer":
        """Create an independent copy of this CronTimer with the same configuration."""
        c = CronTimer(
            expression=self.expression,
            tz=self.tz,
            eager=self.eager,
            max_drift=self.max_drift,
            error_backoff=self.error_backoff,
            headers=dict(self.headers) if self.headers else None,
            token_factory=self.token_factory,
        )
        c.method_name = self.method_name
        return c

    @property
    def _schedule_description(self) -> str:
        return f"cron '{self.expression}' [{self.tz}]"

    def _seconds_until_next(self, now: datetime) -> float:
        """Seconds from ``now`` (a tz-aware datetime) until the next scheduled fire."""
        nxt = croniter(self.expression, now).get_next(datetime)
        return (nxt - now).total_seconds()

    async def _timer_loop(self) -> None:
        """Fire the method at each cron occurrence until stopped."""
        if self.eager:
            await self._execute_method()

        while self.is_running:
            try:
                now = datetime.now(self._tzinfo)
                sleep_time = self._seconds_until_next(now)

                if sleep_time > 0:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_time)
                        # Stop event was set during the wait.
                        break
                    except TimeoutError:
                        # Reached the scheduled time.
                        pass

                if self._stop_event.is_set():
                    break

                await self._execute_method()

            except Exception as e:
                logger.error(f"Error in cron loop for {self.method_name}: {e}")
                self.error_count += 1
                await asyncio.sleep(self.error_backoff)

    def get_stats(self) -> dict[str, Any]:
        stats = super().get_stats()
        stats.pop("interval", None)
        stats["expression"] = self.expression
        stats["tz"] = self.tz
        return stats


def cron(
    expression: str,
    tz: str = "UTC",
    eager: bool = False,
    headers: dict[str, str] | None = None,
    token_factory: Callable[[], str] | None = None,
    *,
    distributed: bool = False,
    bucket: str = "cron_locks",
    lease_ttl: float = 300.0,
    no_overlap: bool = True,
    **kwargs: Any,
) -> Callable:
    """
    Decorator to run a service method on a cron schedule.

    Args:
        expression: Cron expression (e.g. "0 9 * * *") or named schedule (e.g. "@daily").
        tz: IANA timezone the expression is evaluated in (default "UTC").
        eager: If True, run once on service start in addition to the schedule.
        headers: Optional headers passed in WorkerContext.
        token_factory: Optional callable returning a bearer token.
        distributed: If True, coordinates across service replicas using cliffracer-kv.
        bucket: Key-Value bucket name for distributed locks and execution records.
        lease_ttl: Time-to-live for distributed execution leases and records in seconds.
        no_overlap: If True, prevents concurrent overlapping runs of the job across replicas.
        **kwargs: Additional CronTimer or DistributedCronTimer options.

    Example:
        @cron("0 9 * * *")                       # 09:00 UTC daily (local)
        async def morning_report(self):
            ...

        @cron("*/15 * * * *", distributed=True)  # every 15 min, leader-elected across replicas
        async def sync_data(self):
            ...
    """

    refuse_bare_use(expression, "cron", '@cron("0 3 * * *")')

    def decorator(method: Callable) -> Callable:
        if distributed:
            from .distributed import DistributedCronTimer

            cron_timer: CronTimer = DistributedCronTimer(
                expression,
                tz=tz,
                eager=eager,
                headers=headers,
                token_factory=token_factory,
                distributed=True,
                bucket=bucket,
                lease_ttl=lease_ttl,
                no_overlap=no_overlap,
                **kwargs,
            )
        else:
            cron_timer = CronTimer(
                expression,
                tz=tz,
                eager=eager,
                headers=headers,
                token_factory=token_factory,
                **kwargs,
            )
        return cron_timer(method)

    return decorator
