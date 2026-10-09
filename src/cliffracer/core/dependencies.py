"""Dependency health: per-dependency round trips that can make a service unhealthy.

Every declared dependency represents a required service dependency. If a dependency
cannot be reached, the service reports itself as unhealthy.

A dependency check must be a callable that performs an active round trip and raises
on failure.

This module guarantees two properties:
* A hung dependency must not hang /health: every check is bounded by its own timeout. A probe
  that waits cooperatively is cancelled when its timeout passes and the call returns then, WITHOUT
  waiting for the cancellation to finish (a probe slow to honour it is left to finish, and a new
  probe is not started for that dependency until it has). A probe that never yields to the event
  loop (a blocking sync call inside `async def`) cannot be interrupted by anything: the endpoint
  waits for it, and it is reported as failed, never ok, because it overran its timeout.
* A broken check must not take the endpoint down: probe exceptions are caught and
  reported in the health payload rather than resulting in an unhandled 500 error.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from loguru import logger

from .decorators import refuse_bare_use
from .error_text import exception_text
from .exceptions import ConfigurationError

# A check is any zero-argument awaitable. It signals failure by RAISING; a
# return value is ignored, so a check that returns False is not a failure --
# the raising convention is the one every client library already follows.
DependencyProbe = Callable[[], Awaitable[Any]]

DEFAULT_TIMEOUT = 2.0

#: The keys a dependency's entry in the health payload is written with, which a declared `detail`
#: shares the entry with and so cannot use.
RESERVED_DETAIL_KEYS = ("ok", "error", "latency_ms")


def _refuse_detail_keys_the_payload_writes(name: str, detail: Mapping[str, Any]) -> None:
    """Refuse a `detail` key the health payload writes itself, at declaration.

    A dependency's entry starts as a copy of `detail` and is then written with `ok`, `error` and
    `latency_ms`, so a declared key of that name would be silently replaced by the framework's
    value and its own dropped.
    """
    clashing = [key for key in RESERVED_DETAIL_KEYS if key in detail]
    if clashing:
        raise ConfigurationError(
            f"dependency {name!r}: detail key(s) {clashing} are written by the health payload "
            f"itself (ok, error and latency_ms) and would be overwritten. Rename them."
        )


def _require_a_usable_timeout(name: str, timeout: Any) -> None:
    """Refuse a timeout that could never let the probe run, at declaration.

    A bound of zero or less is spent before the probe takes a step, so the
    dependency would be reported unhealthy forever as "timed out after 0s" for a
    probe that never ran, and `/health` would answer 503 with no log saying why.
    A bound that is not finite is no bound at all.
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ConfigurationError(
            f"dependency {name!r}: timeout must be a positive, finite number of seconds, "
            f"got {timeout!r}"
        )


# `eq=False`: a dependency is its own identity (the abandoned-probe table is keyed by it),
# which also keeps it hashable; field-by-field equality would compare probes and details.
@dataclass(frozen=True, eq=False)
class Dependency:
    """One thing a service needs, and how to find out whether it has it.

    `timeout` must be a positive, finite number of seconds, or `ConfigurationError`
    names the dependency. `detail` is copied, one level deep, into a read-only mapping: changing
    the mapping a declaration was built from does not change what `/health` reports, but a list or
    dict held as one of its values is still the caller's.
    """

    name: str
    probe: DependencyProbe
    timeout: float = DEFAULT_TIMEOUT
    # Free-form, surfaced in the payload verbatim: the address or database name, so an
    # operator reading a failure knows WHICH postgres could not be reached.
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_a_usable_timeout(self.name, self.timeout)
        _refuse_detail_keys_the_payload_writes(self.name, self.detail)
        object.__setattr__(self, "detail", MappingProxyType(dict(self.detail)))


def dependency(
    name: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    **detail: Any,
) -> Callable[[Callable], Callable]:
    """Mark a method as a dependency round trip.

        class Api(CliffracerService):
            @dependency("postgres", timeout=1.0, database="jorbo")
            async def _check_db(self):
                async with self.pool.acquire() as conn:
                    await conn.execute("SELECT 1")

    `timeout` must be a positive, finite number of seconds. The `detail` keywords
    are published on `/health` as given, which may be read without authentication:
    pass an address or a database name, never a password or a connection string
    that holds one. The names `ok`, `error` and `latency_ms` are the payload's own
    and are refused as detail keys with a `ConfigurationError`.

    The marker goes on the function, so the same caveat as the HTTP endpoint
    decorators applies: a subclass that overrides a decorated method without
    re-applying the decorator silently drops the check. That is deliberate --
    scanning base classes would resurrect checks a subclass meant to retire --
    but it is the one way to lose a dependency quietly, so re-apply it.
    """

    # Bare-use first: if `name` is the function, the author forgot the
    # arguments entirely and the removed-keyword message would be beside the
    # point.
    refuse_bare_use(name, "dependency", '@dependency("postgres")')
    reject_removed_detail(detail)
    _refuse_detail_keys_the_payload_writes(name, detail)
    _require_a_usable_timeout(name, timeout)

    def decorate(func: Callable) -> Callable:
        func._cliffracer_dependency = {  # type: ignore[attr-defined]
            "name": name,
            "timeout": timeout,
            "detail": detail,
        }
        return func

    return decorate


def reject_removed_detail(detail: dict[str, Any]) -> None:
    """`required=` is not supported and must not become a detail key.

    `**detail` is copied verbatim into the payload; passing `required`
    is explicitly rejected so callers do not expect optional probe semantics.
    """
    if "required" in detail:
        raise TypeError(
            "required= is not supported: every declared dependency decides status. "
            "Drop the argument if the service needs the dependency, or remove "
            "the probe if it does not."
        )


#: Probes that timed out and are still finishing their cancellation, by `id` of the dependency.
_ABANDONED: dict[int, asyncio.Future[Any]] = {}


def _abandon(dep: Dependency, task: asyncio.Future[Any]) -> None:
    """Cancel a probe that timed out without waiting for it, and keep an eye on it.

    The task is held until it finishes (a task nothing references can be collected mid-run),
    and its outcome is read when it does, so a late exception is logged against the dependency
    instead of surfacing as "Task exception was never retrieved".
    """
    key = id(dep)
    _ABANDONED[key] = task
    task.cancel()

    def finished(done: asyncio.Future[Any]) -> None:
        _ABANDONED.pop(key, None)
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:
            logger.warning(
                f"dependency {dep.name!r} probe finished with {type(exc).__name__} "
                f"after it had timed out: {exc}"
            )

    task.add_done_callback(finished)


async def _run_one(dep: Dependency, config: Any = None) -> dict[str, Any]:
    """Run one probe, bounded, and shape the result. Never raises.

    `config` decides whether a failing probe's own words reach the payload;
    see `cliffracer.core.error_text`. Absent, they do not.
    """
    started = time.monotonic()
    result: dict[str, Any] = dict(dep.detail)
    # Read ONCE, so the bound that runs and the bound the message names cannot
    # be two different numbers. Rendering from `dep.timeout` afterwards was a
    # second read: a probe that changed it, or anything that handed `wait_for`
    # something else, left the message describing a budget that was not spent.
    bound = dep.timeout

    def failed(exc: BaseException) -> None:
        """Record a probe that failed on its own terms, whatever it raised.

        The probe's own words, on an endpoint that needs no authentication: a
        connection error carries the address it could not reach, credentials
        included. `ok` already says the dependency is down, which is what a
        probe reads; the text is for an operator, so it goes to the log and
        reaches the payload only when the configuration says it may.
        """
        result["ok"] = False
        service = getattr(config, "name", None)
        (logger.bind(service=service) if service else logger).warning(
            f"dependency {dep.name!r} failed: {type(exc).__name__}: {exc}"
        )
        result["error"] = exception_text(exc, config, generic="probe failed")

    previous = _ABANDONED.get(id(dep))
    if previous is not None:
        # The last probe of this dependency timed out and has not finished cancelling. Starting
        # another would stack one more hung call per health check, so say so instead.
        result["ok"] = False
        result["error"] = "the previous probe has not finished since it timed out"
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        return result

    loop = asyncio.get_running_loop()
    began = loop.time()
    ran_for: list[float] = []

    async def timed() -> Any:
        try:
            pending = dep.probe()
            if not inspect.isawaitable(pending):
                # A plain `def` that does its check and returns, or a forgotten `async`. The
                # unguarded `await` raised "object bool can't be used in 'await' expression",
                # which says nothing about the probe. A `def` that RETURNS an awaitable is a
                # working probe (`lambda: client.ping()`), so this cannot be refused at declaration.
                raise TypeError(
                    f"the probe returned {type(pending).__name__}, which cannot be awaited: "
                    "a probe is called and its result awaited, so declare it `async def` or "
                    "return an awaitable"
                )
            return await pending
        finally:
            ran_for.append(loop.time() - began)

    task: asyncio.Future[Any] | None = None
    outcome: Literal["ok", "failed", "timed_out", "overran"] = "ok"
    try:
        task = asyncio.ensure_future(timed())
        done, _ = await asyncio.wait({task}, timeout=bound)
        if not done:
            # The budget was spent, and only this decides a timeout: a `TimeoutError` the probe
            # raises is the probe's own result and is reported as a failure of the probe. The
            # probe is cancelled and NOT awaited: a probe slow to honour its cancellation (a
            # driver that closes a socket in its `except CancelledError`) must not extend the
            # call past the bound it was given.
            _abandon(dep, task)
            outcome = "timed_out"
        elif task.cancelled():
            # The probe task ended cancelled although nobody here cancelled it: something the probe
            # awaits was cancelled under it (a pool closing on a waiting `acquire`, a client dropping
            # a pending request) and the cancellation came out of the probe. That is a failed probe.
            # It is not the caller going away, which arrives as a `CancelledError` raised by the
            # `wait` above and is re-raised below.
            failed(asyncio.CancelledError("the probe was cancelled before it answered"))
            outcome = "failed"
        else:
            task.result()
            if ran_for and ran_for[0] > bound:
                # Finished, but not within its bound: it did not yield to the loop, so nothing
                # could interrupt it, and the endpoint waited that long. A check that cannot
                # fail in the scenario the bound names is not a check.
                outcome = "overran"
    except asyncio.CancelledError:
        # Not ours to swallow: the caller is going away, and the probe goes with it.
        if task is not None and not task.done():
            task.cancel()
        raise
    except Exception as exc:  # noqa: BLE001 - a failing probe is a result, not a crash
        failed(exc)
        outcome = "failed"

    if outcome == "ok":
        result["ok"] = True
        result["error"] = None
    elif outcome == "timed_out":
        result["ok"] = False
        result["error"] = f"timed out after {bound}s"
    elif outcome == "overran":
        result["ok"] = False
        result["error"] = (
            f"exceeded its {bound}s timeout: took {ran_for[0]:.1f}s without yielding to the "
            f"event loop, so it could not be interrupted"
        )
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


async def check_dependencies(
    deps: list[Dependency], config: Any = None
) -> dict[str, dict[str, Any]]:
    """Run every probe CONCURRENTLY and return one result per dependency.

    Concurrently because the endpoint's worst case should be the slowest
    dependency, not the sum of them: five checks at a 2s timeout run serially
    are a 10s health endpoint, which orchestrators treat as a dead service.
    """
    if not deps:
        return {}
    results = await asyncio.gather(*(_run_one(dep, config) for dep in deps))
    return {dep.name: result for dep, result in zip(deps, results, strict=True)}


def failed_dependencies(results: dict[str, dict[str, Any]]) -> list[str]:
    """Names of the dependencies that failed, in the order of ``results``.

    That is alphabetical by name: every path that builds the list of dependencies
    sorts it, so declaration order is not kept.
    """
    return [name for name, result in results.items() if not result.get("ok")]
