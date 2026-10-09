"""Cron-based timer scheduling for service methods.

Provides ``CronTimer`` and the ``@cron`` decorator for running service methods on
a cron schedule (e.g. "0 9 * * *" for 09:00 daily). Cron expressions are evaluated
in UTC by default; pass ``tz`` to evaluate in another timezone.

``CronTimer`` subclasses :class:`~cliffracer.core.timer.Timer` and reuses its
execution, stop, and discovery machinery — a ``@cron`` handler is registered under
the same ``_cliffracer_timers`` marker as ``@timer``, so the service start/stop
lifecycle needs no special handling.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, croniter

from cliffracer.core.clock import Clock
from cliffracer.core.decorators import refuse_bare_use
from cliffracer.core.timer import Timer

#: How many missed occurrences a wake reports by date; past it the report says it stopped counting.
_MISSED_COUNTED = 1000


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
        token_factory: Callable[[], str | Awaitable[str]] | None = None,
        clock: Clock | None = None,
        deadline: float | None = None,
    ):
        """
        Args:
            expression: A cron expression (e.g. "0 9 * * *") or named schedule
                (e.g. "@hourly"). Validated immediately, including that it names a date
                it can fire on.
            tz: IANA timezone name the expression is evaluated in (default "UTC").
            eager: If True, run the method once on service start, then follow the schedule.
            max_drift: Inherited from Timer (unused by the cron loop, kept for API parity).
            error_backoff: Delay in seconds after an error before resuming the schedule.
            headers: Optional headers passed in WorkerContext.
            token_factory: Optional callable returning a bearer token, or an awaitable of one.
            clock: What the loop reads time and waits through; the real clock when omitted.

        Raises:
            ValueError: If the cron expression or timezone is invalid.
        """
        if not croniter.is_valid(expression):
            raise ValueError(f"Invalid cron expression: {expression!r}")
        try:
            self._tzinfo = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"Invalid timezone: {tz!r}") from e
        # `is_valid` checks the syntax, not that a date exists: "0 0 30 2 *" is valid and names
        # 30 February, so the job would never run and the loop would log an error every backoff.
        try:
            croniter(expression, datetime.now(self._tzinfo)).get_next(datetime)
        except CroniterBadDateError as e:
            raise ValueError(
                f"Cron expression {expression!r} names no date it can fire on (a day that its "
                f"month does not have, such as 30 February or 31 April): the job would never run"
            ) from e

        # interval is unused by the cron loop; pass 0 to satisfy the base initializer.
        super().__init__(
            interval=0,
            eager=eager,
            max_drift=max_drift,
            error_backoff=error_backoff,
            headers=headers,
            token_factory=token_factory,
            clock=clock,
            deadline=deadline,
        )
        self.expression = expression
        self.tz = tz
        #: The target instant (a timestamp) of the occurrence this timer last started, so that a
        #: wall clock stepped back past it cannot make the same occurrence run again.
        self._last_fired: float | None = None

    def _check_interval(self, interval: Any) -> None:
        """A cron timer waits on its expression, so the base interval is a placeholder."""

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
            clock=self.clock,
            deadline=self.deadline,
        )
        c.method_name = self.method_name
        return c

    @property
    def _schedule_description(self) -> str:
        return f"cron '{self.expression}' [{self.tz}]"

    def _next_fire(self, now: datetime) -> tuple[datetime, float]:
        """Return the next future occurrence and its elapsed-time delay.

        Python subtracts two aware datetimes with the same ``tzinfo`` as wall
        times, ignoring an offset change between them. Comparing timestamps
        keeps spring-forward and fall-back waits on the actual timeline.
        """
        schedule = croniter(self.expression, now)
        previous_instant: float | None = None
        now_instant = now.timestamp()

        while True:
            target = schedule.get_next(datetime)
            target_instant = target.timestamp()
            if target_instant > now_instant:
                return target, target_instant - now_instant
            if previous_instant is not None and target_instant <= previous_instant:
                raise RuntimeError("Cron schedule did not advance to a future instant")
            previous_instant = target_instant

    def _report_the_occurrences_missed(self, target: datetime, woke: datetime) -> None:
        """Say so when the loop woke after occurrences beyond the one it waited for.

        A wall clock stepped forward during the wait (or a loop starved of the event loop) leaves
        later occurrences already past by the time the loop wakes. The next wait is computed from
        now, so this replica does not run them; this makes that visible.
        """
        schedule = croniter(self.expression, target)
        missed: list[datetime] = []
        counted_all = False
        while len(missed) < _MISSED_COUNTED:
            following = schedule.get_next(datetime)
            if following.timestamp() > woke.timestamp():
                counted_all = True
                break
            missed.append(following)
        else:
            # The count stops at its cap; one more occurrence before waking means it stopped short.
            counted_all = schedule.get_next(datetime).timestamp() > woke.timestamp()
        if missed:
            uncounted = "" if counted_all else ", and later ones not counted"
            self._log.warning(
                f"cron {self.method_name} {self._schedule_description} woke at {woke.isoformat()}, "
                f"after the occurrence it waited for ({target.isoformat()}) and {len(missed)} more "
                f"({missed[0].isoformat()} to {missed[-1].isoformat()}){uncounted}: the wall clock "
                f"stepped forward or the loop was starved, and this replica does not run those "
                f"occurrences"
            )

    def _seconds_until_next(self, now: datetime) -> float:
        """Seconds from ``now`` until the next scheduled fire on the timeline."""
        return self._next_fire(now)[1]

    async def _wait_for_the_next_occurrence(self) -> tuple[datetime, datetime] | None:
        """Wait until the next occurrence is due and return its target and the time it woke at.

        `None` when the timer was stopped first. The occurrence is recorded as started before it is
        returned, so it is never waited for or run a second time.

        The wait runs on the monotonic clock and the targets are wall-clock times, so the two can
        disagree. A wall clock stepped back DURING the wait has not reached the target when the wait
        ends: firing then would run the occurrence before its time, so the wait goes round again for
        the remainder. A clock stepped back AFTER an occurrence started can put the next search
        before the occurrence it just ran, which would be found again: the search starts no earlier
        than the last target, so each occurrence runs once. A wall clock stepped FORWARD past later
        occurrences leaves them unrun on this replica, and the wait says which, once, before it returns. Both loops
        wait here, so neither can fire an occurrence early or twice, or skip some without saying so.
        """
        while self.is_running:
            now = self.clock.now(self._tzinfo)
            searching_from = now
            if self._last_fired is not None and self._last_fired > now.timestamp():
                searching_from = datetime.fromtimestamp(self._last_fired, self._tzinfo)
            target, _ = self._next_fire(searching_from)
            sleep_time = target.timestamp() - now.timestamp()

            if sleep_time > 0 and await self.clock.wait(self._stop_event, sleep_time):
                # Stop event was set during the wait.
                return None

            if self._stop_event.is_set():
                return None

            woke = self.clock.now(self._tzinfo)
            if woke.timestamp() < target.timestamp():
                continue
            self._last_fired = target.timestamp()
            # Said here, where both loops wait, so that neither can leave a wall clock stepped
            # forward unreported: the occurrences it jumped over are not run by this replica.
            self._report_the_occurrences_missed(target, woke)
            return target, woke
        return None

    async def _timer_loop(self) -> None:
        """Fire the method at each cron occurrence until stopped."""
        if self.eager:
            # Handled as a scheduled firing's error is below.
            try:
                await self._execute_method()
            except Exception as e:
                self._log.error(f"Error in cron loop for {self.method_name}: {e}")
                self.error_count += 1
                await self._back_off()

        while self.is_running:
            try:
                occurrence = await self._wait_for_the_next_occurrence()
                if occurrence is None:
                    break
                await self._execute_method()

            except Exception as e:
                self._log.error(f"Error in cron loop for {self.method_name}: {e}")
                self.error_count += 1
                await self._back_off()

    def get_stats(self) -> dict[str, Any]:
        stats = super().get_stats()
        stats.pop("interval", None)
        stats["expression"] = self.expression
        stats["tz"] = self.tz
        return stats


_DEFAULT_BUCKET = "cron_locks"
_DEFAULT_LEASE_TTL = 300.0


def cron(
    expression: str,
    tz: str = "UTC",
    eager: bool = False,
    headers: dict[str, str] | None = None,
    token_factory: Callable[[], str | Awaitable[str]] | None = None,
    *,
    distributed: bool = False,
    bucket: str = _DEFAULT_BUCKET,
    lease_ttl: float = _DEFAULT_LEASE_TTL,
    no_overlap: bool = True,
    **kwargs: Any,
) -> Callable:
    """
    Decorator to run a service method on a cron schedule.

    Args:
        expression: Cron expression (e.g. "0 9 * * *") or named schedule (e.g. "@daily").
        tz: IANA timezone the expression is evaluated in (default "UTC").
        eager: If True, run once on service start in addition to the schedule. With `distributed=True`
            it runs once per cluster for as long as the bucket keeps the `.eager` key, not on every
            start: its TTL, `max(lease_ttl, 300)` seconds for a bucket the timer creates. A restart
            or a rolling deploy inside that window does not run it again, on this replica or any
            other, and on a bucket with no TTL it never runs again. Work that must run after every
            start belongs in the service's `on_startup`.
        headers: Optional headers passed in WorkerContext.
        token_factory: Optional callable returning a bearer token, or an awaitable of one.
        distributed: If True, coordinates across service replicas using cliffracer-kv.
        bucket: Key-Value bucket name for distributed locks and execution records.
        lease_ttl: How long, in seconds, a running lease is honoured before another run starts over
            it. The lease and the interval records are keys in the bucket and live its TTL,
            `max(lease_ttl, 300)` seconds for a bucket the timer creates and whatever an existing
            bucket has.
        no_overlap: If True, prevents concurrent overlapping runs of the job across replicas.
            `bucket`, `lease_ttl` and `no_overlap` apply only with `distributed=True`; a different
            value without it raises `ValueError`, since it would be ignored.
        **kwargs: Additional CronTimer or DistributedCronTimer options, among them
            `deadline`: the seconds each firing may run before it is cancelled and counted as an
            error, as `@timer`'s. On a distributed job with `no_overlap` it may not exceed
            `lease_ttl`.

    Example:
        @cron("0 9 * * *")                       # 09:00 UTC daily
        async def morning_report(self):
            ...

        @cron("*/15 * * * *", distributed=True)  # every 15 min, leader-elected across replicas
        async def sync_data(self):
            ...
    """

    refuse_bare_use(expression, "cron", '@cron("0 3 * * *")')
    if not distributed and (
        bucket != _DEFAULT_BUCKET or lease_ttl != _DEFAULT_LEASE_TTL or no_overlap is not True
    ):
        raise ValueError(
            "@cron: bucket, lease_ttl and no_overlap apply only to a distributed cron, and "
            "without distributed=True they would be ignored, leaving the job to run on every "
            "replica. Pass distributed=True to coordinate across replicas, or drop them."
        )

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
