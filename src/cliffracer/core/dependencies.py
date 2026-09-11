"""Dependency health: per-dependency round trips that can make a service unhealthy.

Every declared dependency represents a required service dependency. If a dependency
cannot be reached, the service reports itself as unhealthy.

A dependency check must be a callable that performs an active round trip and raises
on failure.

This module guarantees two properties:
* A hung dependency must not hang /health: every check is bounded by its own timeout.
* A broken check must not take the endpoint down: probe exceptions are caught and
  reported in the health payload rather than resulting in an unhandled 500 error.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .decorators import refuse_bare_use

# A check is any zero-argument awaitable. It signals failure by RAISING; a
# return value is ignored, so a check that returns False is not a failure --
# the raising convention is the one every client library already follows.
DependencyProbe = Callable[[], Awaitable[Any]]

DEFAULT_TIMEOUT = 2.0


@dataclass(frozen=True)
class Dependency:
    """One thing a service needs, and how to find out whether it has it."""

    name: str
    probe: DependencyProbe
    timeout: float = DEFAULT_TIMEOUT
    # Free-form, surfaced in the payload: the address or database name, so an
    # operator reading a failure knows WHICH postgres could not be reached.
    detail: dict[str, Any] = field(default_factory=dict)


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


async def _run_one(dep: Dependency) -> dict[str, Any]:
    """Run one probe, bounded, and shape the result. Never raises."""
    started = time.monotonic()
    result: dict[str, Any] = dict(dep.detail)
    try:
        await asyncio.wait_for(dep.probe(), timeout=dep.timeout)
    except TimeoutError:
        # A timeout is a failure with a DIFFERENT cause from a refusal, and the
        # two want different fixes -- a slow dependency and an absent one look
        # identical in a boolean.
        result["ok"] = False
        result["error"] = f"timed out after {dep.timeout}s"
    except asyncio.CancelledError:
        # Not ours to swallow: the caller is going away.
        raise
    except Exception as exc:  # noqa: BLE001 - a failing probe is a result, not a crash
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    else:
        result["ok"] = True
        result["error"] = None
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


async def check_dependencies(deps: list[Dependency]) -> dict[str, dict[str, Any]]:
    """Run every probe CONCURRENTLY and return one result per dependency.

    Concurrently because the endpoint's worst case should be the slowest
    dependency, not the sum of them: five checks at a 2s timeout run serially
    are a 10s health endpoint, which orchestrators treat as a dead service.
    """
    if not deps:
        return {}
    results = await asyncio.gather(*(_run_one(dep) for dep in deps))
    return {dep.name: result for dep, result in zip(deps, results, strict=True)}


def failed_dependencies(results: dict[str, dict[str, Any]]) -> list[str]:
    """Names of the dependencies that failed, in declaration order."""
    return [name for name, result in results.items() if not result.get("ok")]
