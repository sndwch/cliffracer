"""A probe's timeout is a bound the call keeps, and only the budget expiring is a "timeout".

`_run_one` used `asyncio.wait_for` and read any `TimeoutError` as the budget expiring, so:
- a probe that raised a `TimeoutError` of its own (a driver's, a socket's) was recorded as
  "timed out after 30.0s", naming a budget nothing waited for and discarding the probe's words;
- a probe slow to honour its cancellation held the call for the cancellation time, well past
  its timeout, with the payload claiming the timeout;
- a probe that blocked the event loop finished eventually and was recorded `ok: True` after many
  times its budget.
The probe now runs as a task: it is waited on for `timeout`; on expiry it is cancelled and not
awaited; one that overran without yielding is reported failed; any exception it raises, a
`TimeoutError` included, is its own failure.
"""

import asyncio
import time

import pytest
from loguru import logger

from cliffracer.core import dependencies
from cliffracer.core.dependencies import Dependency, _run_one

pytestmark = pytest.mark.unit


class _Exposing:
    expose_internal_errors = True


async def _settle() -> None:
    """Let every probe a test abandoned finish, so no task outlives the test."""
    abandoned = getattr(dependencies, "_ABANDONED", {})
    await asyncio.gather(*list(abandoned.values()), return_exceptions=True)


async def _run(probe, timeout: float, config=None) -> tuple[dict, float]:
    began = time.monotonic()
    result = await _run_one(Dependency(name="db", probe=probe, timeout=timeout), config)
    return result, time.monotonic() - began


async def test_a_timeout_the_probe_raised_itself_is_its_own_failure_not_ours():
    async def probe() -> None:
        raise TimeoutError("driver: connect timed out after 0.011s")

    result, _ = await _run(probe, timeout=30.0, config=_Exposing())

    assert result["ok"] is False
    assert "30.0" not in result["error"], result
    assert "driver: connect timed out after 0.011s" in result["error"], result
    # Upper bound. CI p99 0.2 ms (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); 5000x p99;
    # below 30000 ms (the 30 s timeout).
    assert result["latency_ms"] < 1000


async def test_the_budget_expiring_is_still_a_timeout_naming_the_budget():
    async def probe() -> None:
        await asyncio.sleep(30)

    result, wall = await _run(probe, timeout=0.1)

    assert (result["ok"], result["error"]) == (False, "timed out after 0.1s")
    # Upper bound. CI p99 0.101 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.1 s,
    # 1525x the overshoot; below 30 s (the probe's sleep(30)).
    assert wall < 1.0


async def test_a_probe_slow_to_honour_its_cancellation_does_not_extend_the_call():
    release = asyncio.Event()

    async def probe() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await release.wait()  # a driver closing its socket on cancel, still not done
            raise

    result, wall = await _run(probe, timeout=0.1)
    release.set()
    await _settle()

    assert result["error"] == "timed out after 0.1s"
    # Upper bound. CI p99 0.101 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.1 s,
    # 1079x the overshoot.
    assert wall < 0.8, f"the call waited {wall:.2f}s for a probe's cancellation"


async def test_a_probe_that_blocks_the_event_loop_is_reported_failed_not_ok():
    async def probe() -> None:
        time.sleep(0.4)  # a synchronous driver call inside `async def`

    result, _ = await _run(probe, timeout=0.05)

    assert result["ok"] is False
    assert result["error"].startswith("exceeded its 0.05s timeout"), result


async def test_CONTROL_a_probe_inside_its_budget_is_ok_even_when_it_takes_most_of_it():
    async def probe() -> None:
        await asyncio.sleep(0.05)

    result, _ = await _run(probe, timeout=1.0)

    assert (result["ok"], result["error"]) == (True, None)


async def test_CONTROL_a_probe_that_raises_is_a_failure_and_the_words_stay_hidden_by_default():
    async def probe() -> None:
        raise ConnectionError("postgres://user:secret@db:5432 refused")

    result, _ = await _run(probe, timeout=1.0)

    assert result["ok"] is False
    assert "secret" not in result["error"], result


async def test_an_exception_a_timed_out_probe_raises_late_is_logged_not_lost():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")

    async def probe() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise ValueError("cleanup failed after the timeout") from None

    try:
        await _run(probe, timeout=0.05)
        await _settle()
    finally:
        logger.remove(sink)

    assert any("cleanup failed after the timeout" in line and "'db'" in line for line in lines), (
        lines
    )


async def test_a_new_probe_is_not_started_while_the_last_one_is_still_finishing():
    started = 0
    release = asyncio.Event()

    async def probe() -> None:
        nonlocal started
        started += 1
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await release.wait()  # refuses to finish cancelling until told
            raise

    dep = Dependency(name="db", probe=probe, timeout=0.05)
    first = await _run_one(dep)
    second = await _run_one(dep)

    assert first["error"] == "timed out after 0.05s"
    assert second["ok"] is False and "previous probe" in second["error"], second
    assert started == 1, "a second hung probe was stacked on the first"

    release.set()
    await _settle()
    third = await _run_one(dep)
    assert started == 2 and third["error"] == "timed out after 0.05s", (started, third)
    await _settle()


async def test_cancelling_the_caller_cancels_the_probe():
    cancelled = asyncio.Event()

    async def probe() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runner = asyncio.create_task(_run_one(Dependency(name="db", probe=probe, timeout=30.0)))
    await asyncio.sleep(0.05)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner

    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
