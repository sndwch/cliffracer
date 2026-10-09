"""CyanideConfig refuses a delay that cannot be slept, and a header that asks for one is ignored.

`slow_delay`, `raise_delay` and `sleep_timeout_duration` built with a negative or NaN value, and
the same value was then refused at fault time, inside the dispatch hook, by a bare `ValueError`: the
config said it was ready to inject faults and could not. A delay given as a request header is the
caller's text and not the operator's, so it is logged and ignored, and the fault runs with the
configured value.
"""

import math
import time
from unittest.mock import MagicMock

import pytest
from cliffracer_cyanide import CyanideConfig, CyanideExtension
from cliffracer_cyanide.exceptions import CyanideFaultError
from loguru import logger
from pydantic import ValidationError

from cliffracer.core.extension import WorkerContext

pytestmark = pytest.mark.unit

DELAYS = ("slow_delay", "raise_delay", "sleep_timeout_duration")


@pytest.mark.parametrize("field", DELAYS)
@pytest.mark.parametrize("value", [-1.0, -1e-9, math.nan, math.inf, -math.inf])
def test_a_delay_that_cannot_be_slept_is_refused_when_the_config_is_built(field, value):
    with pytest.raises(ValidationError) as refused:
        CyanideConfig(enabled=True, **{field: value})

    assert [e["loc"] for e in refused.value.errors()] == [(field,)]
    assert "finite number of seconds" in str(refused.value)


@pytest.mark.parametrize("field", DELAYS)
@pytest.mark.parametrize("value", [0, 0.0, 0.25, 3600.0])
def test_CONTROL_a_delay_that_can_be_slept_is_accepted(field, value):
    assert getattr(CyanideConfig(enabled=True, **{field: value}), field) == value


def test_the_environment_is_checked_like_an_argument(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_CYANIDE_SLOW_DELAY", "-3")
    with pytest.raises(ValidationError):
        CyanideConfig()
    monkeypatch.setenv("CLIFFRACER_CYANIDE_SLOW_DELAY", "nan")
    with pytest.raises(ValidationError):
        CyanideConfig()


def test_the_extension_refuses_a_bad_keyword_it_is_given():
    with pytest.raises(ValidationError):
        CyanideExtension(enabled=True, raise_delay=-1.0)
    with pytest.raises(ValidationError):
        CyanideExtension(config=CyanideConfig(enabled=True), sleep_timeout_duration=math.nan)


@pytest.mark.parametrize("value", [-1.0, math.nan, math.inf])
async def test_a_delay_passed_to_a_fault_directly_is_refused_by_name(value):
    ext = CyanideExtension(enabled=True)

    with pytest.raises(ValueError, match="delay must be a finite number of seconds"):
        await ext.slow(delay=value)
    with pytest.raises(ValueError, match="delay must be a finite number of seconds"):
        await ext.raise_after_delay(delay=value)
    with pytest.raises(ValueError, match="duration must be a finite number of seconds"):
        await ext.sleep_past_timeout(duration=value)


async def test_a_bool_passed_to_a_fault_directly_is_not_a_delay():
    """`True` is an `int` to `isinstance`, and a delay of one second is not what it means."""
    ext = CyanideExtension(enabled=True)

    with pytest.raises(ValueError, match="delay must be a finite number of seconds"):
        await ext.slow(delay=True)
    with pytest.raises(ValueError, match="duration must be a finite number of seconds"):
        await ext.sleep_past_timeout(duration=False)


def _context(mode: str, **headers: str) -> WorkerContext:
    return WorkerContext(
        kind="rpc",
        subject="svc.rpc.work",
        headers={"x-cyanide-mode": mode, **headers},
        correlation_id="c-1",
        payload={},
        raw=MagicMock(),
    )


@pytest.fixture
def warnings():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    yield lines
    logger.remove(sink)


@pytest.mark.parametrize("bad", ["abc", "-5", "nan", "inf", ""])
@pytest.mark.parametrize(
    ("mode", "header"),
    [("slow", "x-cyanide-delay"), ("sleep_past_timeout", "x-cyanide-duration")],
)
async def test_a_header_that_is_not_a_delay_is_ignored_and_the_configured_one_is_used(
    mode, header, bad, warnings
):
    ext = CyanideExtension(enabled=True, slow_delay=0.2, sleep_timeout_duration=0.2)
    started = time.monotonic()

    await ext.worker_setup(_context(mode, **{header: bad}))

    # The configured delay, not zero: a header that was ignored by running with no delay at all
    # would pass a test whose configured delay is also zero.
    elapsed = time.monotonic() - started
    # Upper bound. CI p99 0.201 s (run 4712: eric-7, CPython 3.12.15, n=200, nearest-rank p99); wait
    # 0.2 s, 1965x the overshoot.
    # Lower bound: the configured 0.2 s delay less clock slack; a header run with no delay lands
    # near 0. Load can only lengthen it.
    assert 0.19 <= elapsed < 3.0, elapsed
    assert [i.mode for i in ext.injections()] == [mode], "the fault still ran, and was recorded"
    assert any(header in line for line in warnings), warnings


@pytest.mark.parametrize("bad", ["abc", "-5", "nan"])
async def test_a_raising_fault_with_a_bad_delay_header_still_raises_its_fault(bad, warnings):
    ext = CyanideExtension(enabled=True, raise_delay=0.2)
    started = time.monotonic()

    with pytest.raises(CyanideFaultError):
        await ext.worker_setup(_context("raise_after_delay", **{"x-cyanide-delay": bad}))

    # Lower bound: the configured 0.2 s delay less clock slack; a delay skipped for the bad header
    # lands near 0. Load can only lengthen it.
    assert time.monotonic() - started >= 0.19, "the configured delay was not slept"

    assert any("x-cyanide-delay" in line for line in warnings), warnings


@pytest.mark.parametrize(
    ("mode", "header"),
    [("slow", "x-cyanide-delay"), ("sleep_past_timeout", "x-cyanide-duration")],
)
async def test_CONTROL_a_header_that_is_a_delay_is_used(mode, header, warnings):
    ext = CyanideExtension(enabled=True, slow_delay=0.0, sleep_timeout_duration=0.0)
    started = time.monotonic()

    await ext.worker_setup(_context(mode, **{header: "0.15"}))

    # Lower bound: the header's 0.15 s less clock slack; with 0 configured, only the header can make
    # it wait. Load can only lengthen it.
    assert time.monotonic() - started >= 0.14
    assert not [line for line in warnings if header in line], warnings
