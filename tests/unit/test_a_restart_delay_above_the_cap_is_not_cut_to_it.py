"""The restart backoff doubles up to a cap of 60 s, and never below the delay it was set to.

The cap was a constant 60, so `restart_delay=120` waited 120 s, then 60 s, then 60 s: a backoff
that shrinks after the first crash. The cap is the larger of 60 s and the configured delay.
"""

import pytest

from tests.unit.test_service_runner_restart_loop import _delays, _scripted

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_a_restart_delay_above_sixty_seconds_does_not_shrink(monkeypatch):
    assert await _delays(monkeypatch, _scripted([], restart_delay=120.0), waits=3) == [
        120.0,
        120.0,
        120.0,
    ]


@pytest.mark.asyncio
async def test_a_restart_delay_below_sixty_seconds_still_doubles_to_sixty(monkeypatch):
    """The control: the cap is still 60 s for a delay below it, and the delay doubles up to it.

    20 s doubles to 40 s before the cap: a delay one doubling from the cap would reach it by any
    growth at all, and so could not tell doubling from tripling.
    """
    assert await _delays(monkeypatch, _scripted([], restart_delay=20.0), waits=3) == [
        20.0,
        40.0,
        60,
    ]
