"""Stop consuming a durable listener while a dependency it names is down.

A listener declared with `pause_when_down=("postgres",)` would otherwise keep taking messages it
cannot handle while `postgres` is down: each one fails, is NAKed into backoff and, at the delivery
limit, dead-lettered. `ListenerPauses` probes the dependencies such listeners name, in the
background, and stops consumption of a listener while any of its dependencies is down, so its
messages wait in the stream. The container does the stopping: it drops the replica's interest in
the durable (a push subscription is unsubscribed, a pull loop stops fetching) and binds it again
on resume. The durable and its messages stay on the server, and a message is not delivered, so
spends no delivery attempt, while nobody is bound.

A dependency counts as down after `dependency_pause_after` failed probes in a row and as up again
after `dependency_resume_after` passes in a row; probes run every `dependency_probe_interval`
seconds, so each state lasts at least one interval. Nothing resumes a listener on a timer: a
dependency that never comes back keeps its listeners paused, with a warning every
`WARN_EVERY` intervals.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .dependencies import Dependency, check_dependencies
from .jetstream import StreamSpec
from .subjects import subjects_overlap

#: Probe intervals between the warnings about a listener that is still paused.
WARN_EVERY = 10


@dataclass
class _DependencyState:
    """What the background probe has seen of one dependency."""

    down: bool = False
    failures: int = 0
    passes: int = 0
    last_error: str | None = None


@dataclass
class _Paused:
    """One paused listener: since when, on which dependencies, and how many probes ago."""

    since_wall: datetime
    since: float
    dependencies: tuple[str, ...]
    intervals: int = 0


@dataclass
class ListenerPauses:
    """The background probe and the pause state of the listeners declared with `pause_when_down`.

    `pause(subject)` and `resume(subject)` are the container's: they drop and restore the
    replica's interest in the listener's durable. `notify(hook, subject, dependencies)` runs the
    extensions' `on_listener_paused` / `on_listener_resumed` hooks.
    """

    listeners: dict[str, tuple[str, ...]]
    dependencies: Callable[[], list[Dependency]]
    config: Any
    pause: Callable[[str], Awaitable[None]]
    resume: Callable[[str], Awaitable[None]]
    logger: Any
    notify: Callable[[str, str, tuple[str, ...]], Awaitable[None]] | None = None
    streams: list[StreamSpec] = field(default_factory=list)
    clock: Callable[[], float] = time.monotonic
    _states: dict[str, _DependencyState] = field(default_factory=dict, init=False)
    _paused: dict[str, _Paused] = field(default_factory=dict, init=False)

    @property
    def named(self) -> list[str]:
        """Every dependency some listener names, sorted."""
        return sorted({name for names in self.listeners.values() for name in names})

    @property
    def paused(self) -> dict[str, dict[str, Any]]:
        """The paused listeners, by subject, as `/health` reports them."""
        now = self.clock()
        return {
            subject: {
                "since": paused.since_wall.isoformat(),
                "seconds": round(now - paused.since, 3),
                "dependencies": list(paused.dependencies),
            }
            for subject, paused in sorted(self._paused.items())
        }

    def is_down(self, name: str) -> bool:
        """Whether the background probe currently counts `name` as down."""
        state = self._states.get(name)
        return state is not None and state.down

    async def run(self, is_running: Callable[[], bool]) -> None:
        """Probe and pause or resume, every `dependency_probe_interval` seconds, while running."""
        while is_running():
            await self.tick()
            await asyncio.sleep(self.config.dependency_probe_interval)

    async def tick(self) -> None:
        """One round: probe each named dependency, then pause or resume what that changes."""
        await self._probe()
        for subject in sorted(self.listeners):
            down = tuple(name for name in self.listeners[subject] if self.is_down(name))
            if down and subject not in self._paused:
                await self._pause(subject, down)
            elif not down and subject in self._paused:
                await self._resume(subject)
            elif down:
                self._paused[subject].dependencies = down
        for subject, paused in sorted(self._paused.items()):
            paused.intervals += 1
            if paused.intervals % WARN_EVERY == 0:
                self._warn_still_paused(subject, paused)

    async def _probe(self) -> None:
        wanted = set(self.named)
        deps = [dep for dep in self.dependencies() if dep.name in wanted]
        results = await check_dependencies(deps, self.config)
        for name in sorted(wanted):
            result = results.get(name)
            state = self._states.setdefault(name, _DependencyState())
            if result is not None and result.get("ok"):
                state.passes += 1
                state.failures = 0
                if state.down and state.passes >= self.config.dependency_resume_after:
                    state.down = False
                    self.logger.info(
                        f"dependency {name!r} is up again after {state.passes} passing probes"
                    )
            else:
                state.failures += 1
                state.passes = 0
                state.last_error = (
                    "no such dependency is declared"
                    if result is None
                    else str(result.get("error") or "probe failed")
                )
                if not state.down and state.failures >= self.config.dependency_pause_after:
                    state.down = True
                    self.logger.warning(
                        f"dependency {name!r} is down after {state.failures} failed probes: "
                        f"{state.last_error}"
                    )

    async def _pause(self, subject: str, down: tuple[str, ...]) -> None:
        try:
            await self.pause(subject)
        except Exception as exc:  # noqa: BLE001 - tried again on the next round
            self.logger.error(
                f"could not pause listener {subject!r} while {list(down)} is down: "
                f"{type(exc).__name__}: {exc}; trying again in "
                f"{self.config.dependency_probe_interval:g}s"
            )
            return
        self._paused[subject] = _Paused(datetime.now(UTC), self.clock(), down)
        errors = "; ".join(f"{name}: {self._states[name].last_error}" for name in down)
        self.logger.warning(
            f"paused listener {subject!r}: {list(down)} is down ({errors}). Its messages wait in "
            f"the stream until it resumes."
        )
        if self.notify is not None:
            await self.notify("on_listener_paused", subject, down)

    async def _resume(self, subject: str) -> None:
        try:
            await self.resume(subject)
        except Exception as exc:  # noqa: BLE001 - tried again on the next round
            self.logger.error(
                f"could not resume listener {subject!r}: {type(exc).__name__}: {exc}; trying "
                f"again in {self.config.dependency_probe_interval:g}s"
            )
            return
        paused = self._paused.pop(subject)
        self.logger.info(
            f"resumed listener {subject!r} after {self.clock() - paused.since:.1f}s paused: "
            f"{list(self.listeners[subject])} up"
        )
        if self.notify is not None:
            await self.notify("on_listener_resumed", subject, self.listeners[subject])

    def _warn_still_paused(self, subject: str, paused: _Paused) -> None:
        limits = self._retention_limits(subject)
        self.logger.warning(
            f"listener {subject!r} is still paused, {self.clock() - paused.since:.0f}s since "
            f"{paused.since_wall.isoformat()}, on {list(paused.dependencies)}. {limits}"
        )

    def _retention_limits(self, subject: str) -> str:
        """What the stream holding the listener's messages may drop while it waits."""
        holding = [
            spec
            for spec in self.streams
            if any(subjects_overlap(pattern, subject) for pattern in spec.subjects)
        ]
        if not holding:
            return "Its stream is not declared here, so its retention limits are not known."
        aged = [spec for spec in holding if spec.max_age_seconds]
        if not aged:
            return (
                f"Stream {', '.join(s.name for s in holding)} sets no max age, so its messages "
                f"wait."
            )
        return "Messages older than the stream's max age are removed while it waits: " + ", ".join(
            f"{s.name} max_age_seconds={s.max_age_seconds:g}" for s in aged
        )


def paused_listeners(pauses: ListenerPauses | None) -> dict[str, Any]:
    """The `/health` block for the paused listeners: `paused_listeners`, or nothing when none is."""
    if pauses is None or not pauses.paused:
        return {}
    return {"paused_listeners": pauses.paused}
