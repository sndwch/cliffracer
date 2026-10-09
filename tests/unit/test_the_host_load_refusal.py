"""The load refusal fires above its limit, and only above it.

`cliffracer.testing.host_load` turns a duration assertion into a third outcome on a busy
host: not a pass, not a failure, not taken. That is only worth having if the
refusal itself is exercised -- otherwise it is a branch that has never run, in
the one place a test suite cannot notice, since its whole job is to make a test
not run.
"""

from __future__ import annotations

import pytest

from cliffracer.testing import host_load

pytestmark = pytest.mark.unit


def test_it_refuses_above_the_limit(monkeypatch):
    """A skip, naming the load and the limit, not a failure."""
    monkeypatch.setattr(host_load, "one_minute_load", lambda: host_load.LOAD_LIMIT + 0.01)

    with pytest.raises(pytest.skip.Exception) as raised:
        host_load.skip_if_the_host_is_too_busy_to_judge("a burst of probes")

    message = str(raised.value)
    assert "NOT JUDGED" in message, message
    assert "a burst of probes" in message, message
    assert f"{host_load.LOAD_LIMIT:.2f}" in message, message
    assert "NOT a pass and NOT a failure" in message, message


@pytest.mark.parametrize("load", [0.0, 1.0, 2.0])
def test_it_judges_at_or_below_the_limit(monkeypatch, load: float):
    """The boundary is inclusive, so a host exactly at the limit is still judged.

    Asserted rather than left to the comparison operator: `>` and `>=` differ by
    exactly the case a quiet host sits on, and the wrong one skips a fifth of
    ordinary runs.
    """
    monkeypatch.setattr(host_load, "one_minute_load", lambda: load)

    host_load.skip_if_the_host_is_too_busy_to_judge("a measurement")


def test_a_platform_without_a_load_average_is_still_judged(monkeypatch):
    """Refusing there would silently delete the assertion, not caveat it."""
    monkeypatch.setattr(host_load, "one_minute_load", lambda: None)

    host_load.skip_if_the_host_is_too_busy_to_judge("a measurement")


def test_CONTROL_the_real_reading_returns_a_number_on_this_host():
    """So the tests above are not the only thing that ever calls it.

    They all replace `one_minute_load`. If the real one raised or returned
    something unusable, every call site would refuse or crash and nothing here
    would say so.
    """
    load = host_load.one_minute_load()

    assert load is None or isinstance(load, float), repr(load)
    if load is not None:
        assert load >= 0.0, load
