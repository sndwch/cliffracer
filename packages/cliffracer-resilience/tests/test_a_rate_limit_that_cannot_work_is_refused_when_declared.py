"""A rate limit whose numbers cannot work raises where it is declared.

`@rate_limit(calls=3, window=0)` removed the limit: a window of zero (or less) leaves nothing in the
window to count, so every call was allowed and nothing said so. A window that is `nan` or infinite
allowed `calls` and then refused for ever, `calls` of zero or less refused every call, and a bool or a
fraction was taken as a count. The sibling declarations (the circuit breaker, a dependency's timeout,
a bucket) are checked when they are built; this one is too, by `check_a_limit`, in the decorator, in
`RateLimitConfig` and in `ResilienceExtension`'s default limit.
"""

import math

import pytest
from cliffracer_resilience import ResilienceExtension, rate_limit
from cliffracer_resilience.rate_limiter import RateLimitConfig, RateLimitExceeded

from cliffracer.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit

CANNOT_WORK = [
    (0, 60),
    (-1, 60),
    (3, 0),
    (3, -5),
    (3, math.nan),
    (3, math.inf),
    (3, -math.inf),
    (2.5, 60),
    (True, 60),
    (3, True),
    (3, "60"),
    ("3", 60),
    (None, 60),
    (3, None),
]
CAN_WORK = [(1, 0.001), (100, 60), (3, 60.5), (3, 1), (1_000_000, 86_400)]


@pytest.mark.parametrize(("calls", "window"), CANNOT_WORK)
def test_the_decorator_refuses_a_limit_that_cannot_work_where_it_is_applied(calls, window):
    with pytest.raises(ConfigurationError) as caught:
        rate_limit(calls=calls, window=window)

    message = str(caught.value)
    assert "@rate_limit" in message
    assert repr(calls) in message or repr(window) in message


@pytest.mark.parametrize(("calls", "window"), CANNOT_WORK)
def test_a_rate_limit_config_refuses_it_too(calls, window):
    with pytest.raises(ConfigurationError):
        RateLimitConfig(calls=calls, window=window)


@pytest.mark.parametrize(("calls", "window"), CAN_WORK)
def test_CONTROL_a_limit_that_can_work_is_accepted(calls, window):
    RateLimitConfig(calls=calls, window=window)
    rate_limit(calls=calls, window=window)


async def test_CONTROL_a_declared_limit_still_limits():
    @rate_limit(calls=2, window=60)
    async def handler():
        return "ok"

    assert [await handler(), await handler()] == ["ok", "ok"]
    with pytest.raises(RateLimitExceeded):
        await handler()


@pytest.mark.parametrize(("calls", "window"), CANNOT_WORK)
def test_the_extensions_default_limit_is_checked_when_it_is_built(calls, window):
    if calls is None or window is None:
        pytest.skip("covered by the together-or-not-at-all test: one of the two is absent")

    with pytest.raises(ConfigurationError) as caught:
        ResilienceExtension(default_calls=calls, default_window=window)

    assert "default limit" in str(caught.value)


@pytest.mark.parametrize(
    ("kwargs", "given"),
    [({"default_calls": 5}, "default_calls"), ({"default_window": 60.0}, "default_window")],
)
def test_a_default_limit_given_by_halves_is_refused_naming_the_half(kwargs, given):
    with pytest.raises(ConfigurationError) as caught:
        ResilienceExtension(**kwargs)

    assert f"only {given}" in str(caught.value)


def test_CONTROL_both_defaults_or_neither_are_accepted():
    ResilienceExtension(default_calls=5, default_window=60.0)
    ResilienceExtension()
