"""CyanideConfig refuses settings it cannot carry out, and its defaults do what the README says.

Each refusal below has an accepted twin next to it: a test that only checked the
refusal would pass for a config that refused everything.
"""

from __future__ import annotations

import inspect
import itertools
import math
from unittest.mock import MagicMock

import pytest
from cliffracer_cyanide import CyanideConfig, CyanideExtension
from pydantic import ValidationError

from cliffracer import ServiceConfig
from cliffracer.client import ServiceClient
from cliffracer.core.extension import WorkerContext

pytestmark = pytest.mark.unit

WEIGHTS = (
    "slow_weight",
    "drop_reply_weight",
    "raise_after_delay_weight",
    "sleep_past_timeout_weight",
)


@pytest.mark.parametrize("weight", WEIGHTS)
@pytest.mark.parametrize("value", [-0.5, -1e-9, 1.5, 2.0, math.nan, math.inf])
def test_a_weight_that_is_not_a_probability_is_refused(weight, value):
    with pytest.raises(ValidationError) as exc:
        CyanideConfig(**{weight: value})
    # Refused as THAT field: a refusal of the whole config would also name it, in
    # the input it echoes, and would not say which setting to change.
    assert [e["loc"] for e in exc.value.errors()] == [(weight,)]


@pytest.mark.parametrize("weight", WEIGHTS)
@pytest.mark.parametrize("value", [0.0, 0.25, 1.0])
def test_a_weight_that_is_a_probability_is_accepted(weight, value):
    assert getattr(CyanideConfig(**{weight: value}), weight) == value


@pytest.mark.parametrize(("first", "second"), list(itertools.combinations(WEIGHTS, 2)))
def test_weights_that_add_past_one_are_refused_and_say_so(first, second):
    # The reported case: each asked for 80%, and the second could only ever
    # get the 20% the first left it. Every pair, so a weight left out of the
    # sum is found.
    with pytest.raises(ValidationError) as exc:
        CyanideConfig(**{first: 0.8, second: 0.8})
    assert "add up to 1.6" in str(exc.value)


def test_weights_that_add_to_one_are_accepted():
    CyanideConfig(
        slow_weight=0.25,
        drop_reply_weight=0.25,
        raise_after_delay_weight=0.25,
        sleep_past_timeout_weight=0.25,
    )


def test_weights_that_add_to_one_in_decimal_are_accepted():
    """0.2 + 0.4 + 0.3 + 0.1 is 1.0000000000000002 in floating point."""
    weights = {
        "slow_weight": 0.2,
        "drop_reply_weight": 0.4,
        "raise_after_delay_weight": 0.3,
        "sleep_past_timeout_weight": 0.1,
    }
    # Left to right, as the config adds them: `sum()` compensates and gets 1.0.
    assert 0.2 + 0.4 + 0.3 + 0.1 > 1.0, "the sum this test is about has changed"
    CyanideConfig(**weights)


def test_a_mode_that_is_not_a_mode_is_refused_and_the_modes_are_named():
    with pytest.raises(ValidationError) as exc:
        CyanideConfig(enabled=True, mode="slwo")
    text = str(exc.value)
    assert "slwo" in text
    for name in ("slow", "random", "drop_reply", "raise_after_delay", "sleep_past_timeout"):
        assert name in text


@pytest.mark.parametrize(
    "mode",
    [
        None,
        "",
        "random",
        "RANDOM",
        "slow",
        "raise",
        "fault",
        "timeout",
        "drop",
        "Drop-Reply",
        "raise-after-delay",
        "sleep_timeout",
    ],
)
def test_a_mode_that_exists_is_accepted(mode):
    assert CyanideConfig(mode=mode).mode == mode


def test_the_environment_is_checked_like_an_argument(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_CYANIDE_MODE", "slwo")
    with pytest.raises(ValidationError):
        CyanideConfig()
    monkeypatch.setenv("CLIFFRACER_CYANIDE_MODE", "slow")
    monkeypatch.setenv("CLIFFRACER_CYANIDE_SLOW_WEIGHT", "1.5")
    with pytest.raises(ValidationError):
        CyanideConfig()


def test_the_extension_refuses_a_mode_that_is_not_a_mode():
    ext = CyanideExtension(enabled=True)
    with pytest.raises(ValueError, match="slwo"):
        ext.set_mode("slwo")
    with pytest.raises(ValueError, match="slwo"):
        ext.configure_handler("process", "slwo")
    with pytest.raises(ValueError):
        ext.configure_handler("process", "")
    assert ext.active_mode is None
    ext.set_mode("slow")
    ext.configure_handler("process", "drop")
    assert ext.active_mode == "slow"
    ext.set_mode(None)
    assert ext.active_mode is None


def test_the_extension_refuses_a_bad_keyword_it_is_given():
    with pytest.raises(ValidationError):
        CyanideExtension(enabled=True, mode="slwo")
    with pytest.raises(ValidationError):
        CyanideExtension(config=CyanideConfig(slow_weight=0.8), drop_reply_weight=0.8)


async def test_a_caller_cannot_make_the_service_refuse_what_the_config_accepts():
    """A header naming no mode stays a logged no-op: it is the caller's text, not the operator's."""
    ext = CyanideExtension(enabled=True)
    ctx = WorkerContext(
        kind="rpc",
        subject="svc.rpc.process",
        headers={"x-cyanide-mode": "slwo"},
        correlation_id="c-1",
        payload={},
        raw=MagicMock(),
    )
    await ext.worker_setup(ctx)
    assert ext.injections() == []


def test_the_default_sleep_outlasts_the_default_caller_timeouts():
    """`sleep_past_timeout` has to sleep past a caller that has not changed its timeout."""
    client_timeout = inspect.signature(ServiceClient.__init__).parameters["timeout"].default
    service_timeout = ServiceConfig(name="probe").request_timeout
    default = CyanideConfig().sleep_timeout_duration
    assert default > client_timeout
    assert default > service_timeout
