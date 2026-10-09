"""A dependency that names no timeout gets the documented one, and it is small.

`dependencies.py` promises that a hung dependency cannot hang `/health`. Every test of a hung
probe passed an explicit `timeout=`, so what was guarded was that a supplied bound is honoured,
not that the bound a probe gets when it omits one is sane: `DEFAULT_TIMEOUT` could be raised to
600 seconds with the whole suite green, turning every un-annotated probe into a ten-minute hang of
`/health`. The README says five 2-second checks cost about two seconds; the number is pinned here
where an operator would read it.
"""

import asyncio
import time

import pytest

from cliffracer.core.dependencies import (
    DEFAULT_TIMEOUT,
    Dependency,
    _run_one,
    dependency,
)

pytestmark = pytest.mark.unit


def test_the_default_is_the_documented_two_seconds():
    assert DEFAULT_TIMEOUT == 2.0


def test_a_decorated_probe_that_names_no_timeout_gets_the_default():
    class Svc:
        @dependency("postgres")
        async def _check(self) -> None: ...

    marker = Svc._check._cliffracer_dependency  # type: ignore[attr-defined]

    assert marker["timeout"] == DEFAULT_TIMEOUT


def test_a_dependency_built_without_a_timeout_gets_the_default():
    async def probe() -> None: ...

    assert Dependency(name="x", probe=probe).timeout == DEFAULT_TIMEOUT


async def test_a_hung_probe_with_no_explicit_timeout_is_cut_off_at_the_default():
    async def hangs() -> None:
        await asyncio.sleep(3600)

    # ABSOLUTE bounds, not ones derived from DEFAULT_TIMEOUT: a bound that scales with the
    # constant under test would wait as long as a raised default does before failing.
    started = time.monotonic()
    result = await asyncio.wait_for(_run_one(Dependency(name="x", probe=hangs)), timeout=6.0)

    assert result["ok"] is False and result["error"] == f"timed out after {DEFAULT_TIMEOUT}s"
    # Upper bound. CI p99 2 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 2 s, 809x
    # the overshoot; below 6 s (the outer wait_for, so the assert can fire).
    assert time.monotonic() - started < 4.0
