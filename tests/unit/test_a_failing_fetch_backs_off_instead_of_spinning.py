"""A pull loop whose fetch keeps failing waits longer each time, and a loop that works is not slowed.

Any exception from `fetch` other than a timeout (a consumer deleted on the server, a subscription that
is no longer valid, a permission error) was logged at ERROR and retried after
`asyncio.sleep(jetstream_nak_backoff)`. That setting paces redelivery of a NAKed message and may be 0,
and at 0 the loop was a spin that logged an error on every pass: 31,000 attempts in half a second.
The wait is a second at least, doubles to `jetstream_max_backoff`, and starts over after a fetch
that works.
"""

import asyncio
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.errors import NotFoundError

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.dispatch import jetstream as jetstream_module

pytestmark = pytest.mark.unit

#: `asyncio.sleep` as imported, before `_run` records and skips every sleep.
_REAL_SLEEP = asyncio.sleep


def _dispatcher(**config):
    svc = CliffracerService(ServiceConfig(name="pinger", health_port=0, **config))
    return svc.container.dispatcher.jetstream


def _sub(*outcomes):
    """A pull subscription whose successive fetches raise, or return, as listed. Each fetch gives
    the event loop back once, as a fetch over the network does, so a loop that stops waiting
    between attempts runs into the test's own bound rather than freezing the test."""
    sub = MagicMock()
    sub.unsubscribe = AsyncMock()

    async def fetch(*args, **kwargs):
        await _REAL_SLEEP(0)
        outcome = outcomes[min(sub.fetch.await_count - 1, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    sub.fetch = AsyncMock(side_effect=fetch)
    return sub


async def _run(monkeypatch, dispatcher, sub, *, passes: int):
    """Run the loop for `passes` fetches with every sleep recorded and skipped."""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep
    done = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if sub.fetch.await_count >= passes:
            done.set()
        await real_sleep(0)

    monkeypatch.setattr(jetstream_module.asyncio, "sleep", fake_sleep)
    task = asyncio.create_task(
        dispatcher.pull_loop(sub, "pinger", is_running_fn=lambda: not done.is_set())
    )
    await asyncio.wait_for(task, timeout=5)
    return sleeps


async def test_a_fetch_that_keeps_failing_waits_a_second_then_doubles_to_the_cap(monkeypatch):
    dispatcher = _dispatcher(jetstream_nak_backoff=1.0, jetstream_max_backoff=8.0)

    sleeps = await _run(monkeypatch, dispatcher, _sub(NotFoundError()), passes=7)

    assert sleeps[:6] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


async def test_a_nak_backoff_of_zero_does_not_make_the_loop_spin(monkeypatch):
    dispatcher = _dispatcher(jetstream_nak_backoff=0.0, jetstream_max_backoff=60.0)

    sleeps = await _run(monkeypatch, dispatcher, _sub(NotFoundError()), passes=5)

    assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0]


async def test_a_larger_nak_backoff_is_the_base_of_the_fetch_retry(monkeypatch):
    dispatcher = _dispatcher(jetstream_nak_backoff=3.0, jetstream_max_backoff=20.0)

    sleeps = await _run(monkeypatch, dispatcher, _sub(NotFoundError()), passes=5)

    assert sleeps[:4] == [3.0, 6.0, 12.0, 20.0]


async def test_a_max_backoff_below_a_second_still_leaves_a_second_between_attempts(monkeypatch):
    dispatcher = _dispatcher(jetstream_nak_backoff=0.0, jetstream_max_backoff=0.0)

    sleeps = await _run(monkeypatch, dispatcher, _sub(NotFoundError()), passes=4)

    assert sleeps[:3] == [1.0, 1.0, 1.0]


async def test_a_fetch_that_works_starts_the_wait_over(monkeypatch):
    dispatcher = _dispatcher(jetstream_nak_backoff=1.0, jetstream_max_backoff=60.0)
    sub = _sub(NotFoundError(), NotFoundError(), NotFoundError(), [], NotFoundError())

    sleeps = await _run(monkeypatch, dispatcher, sub, passes=5)

    # three failures, an empty fetch (the loop's own short pause), then the first failure again
    assert sleeps[:5] == [1.0, 2.0, 4.0, 0.05, 1.0]


@pytest.mark.parametrize("count", [5, 1], ids=["five-messages", "one-message"])
async def test_a_fetch_that_returns_messages_is_followed_at_once_and_a_failure_waits(
    monkeypatch, count
):
    """Only an empty fetch pauses: after any message, one included, the next fetch is at once,
    after a failure the retry delay."""
    dispatcher = _dispatcher(jetstream_nak_backoff=1.0)
    dispatcher.pull_once = AsyncMock(side_effect=[count, count, NotFoundError()])
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(jetstream_module.asyncio, "sleep", fake_sleep)
    sub = MagicMock()
    sub.unsubscribe = AsyncMock()

    await asyncio.wait_for(
        dispatcher.pull_loop(
            sub, "pinger", is_running_fn=lambda: dispatcher.pull_once.await_count < 3
        ),
        timeout=5,
    )

    assert sleeps == [1.0]


async def test_in_real_time_a_failing_fetch_with_no_nak_backoff_is_a_handful_of_attempts_at_most():
    dispatcher = _dispatcher(jetstream_nak_backoff=0.0)
    sub = _sub(NotFoundError())
    task = asyncio.create_task(dispatcher.pull_loop(sub, "pinger"))
    started = time.monotonic()
    await asyncio.sleep(0.5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # Upper bound. CI p99 0.501 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.5 s,
    # 1316x the overshoot.
    assert time.monotonic() - started < 2
    assert sub.fetch.await_count <= 2, f"{sub.fetch.await_count} attempts in half a second"


async def test_the_error_line_says_when_the_fetch_is_tried_again(monkeypatch):
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="ERROR", format="{message}")
    dispatcher = _dispatcher(jetstream_nak_backoff=1.0)
    try:
        await _run(monkeypatch, dispatcher, _sub(NotFoundError()), passes=2)
    finally:
        logger.remove(sink)

    assert lines and "fetching again in 1s" in lines[0], lines


def test_the_delay_is_a_pure_function_of_the_failure_count():
    dispatcher = _dispatcher(jetstream_nak_backoff=0.5, jetstream_max_backoff=30.0)

    assert [dispatcher.fetch_retry_delay(n) for n in (1, 2, 3, 6, 7, 1000)] == [
        1.0,
        2.0,
        4.0,
        30.0,
        30.0,
        30.0,
    ]


def test_a_fetch_that_fails_without_suspending_still_gives_the_event_loop_back():
    """A fetch on a subscription that is already gone can raise before it awaits anything; then
    the wait between attempts is the loop's only yield. A loop that spins without it cannot time
    itself out, so the bound is a separate process."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "tests.fixtures.pull_loop_process"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("a failing fetch that never suspends froze the pull loop's event loop")
    assert result.returncode == 0, result.stderr
    assert "DONE 1" in result.stdout.split("\n")
