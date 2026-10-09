"""A key function that fails is an error, on the standalone path as on the dispatched one.

`resolve_key` used to swallow a failing key function on the standalone path (a `@rate_limit`
handler called directly, not through the extension) and return the bucket `"global"`, so every
partition merged into one budget with nothing logged; and it guessed a callable's arity by
catching `TypeError`, which called a key function whose own body raised `TypeError` a second time.
The key function's own exception now reaches the caller, once, on both paths.
"""

import pytest
from cliffracer_resilience import rate_limit
from cliffracer_resilience.rate_limiter import RateLimitConfig

pytestmark = pytest.mark.unit


def test_a_key_function_that_raises_is_not_turned_into_the_global_bucket_standalone():
    config = RateLimitConfig(calls=1, window=60.0, key=lambda user_id: 1 / 0)

    with pytest.raises(ZeroDivisionError):
        config.resolve_key(None, user_id="a")


async def test_the_decorated_function_does_not_run_or_count_when_its_key_function_raises():
    ran = 0

    def broken(user_id: str) -> str:
        raise RuntimeError("the key function is broken")

    @rate_limit(calls=1, window=60.0, key=broken)
    async def handler(user_id: str) -> str:
        nonlocal ran
        ran += 1
        return "ran"

    for _ in range(2):  # twice: a swallowed error would admit the first call as "global"
        with pytest.raises(RuntimeError, match="the key function is broken"):
            await handler(user_id="a")

    assert ran == 0


def test_a_key_function_whose_own_body_raises_type_error_is_called_once_standalone():
    calls: list[tuple] = []

    def key(user_id: str) -> str:
        calls.append((user_id,))
        raise TypeError("a bug inside the user's own key function")

    with pytest.raises(TypeError, match="a bug inside"):
        RateLimitConfig(calls=1, window=60.0, key=key).resolve_key(None, user_id="a")

    assert calls == [("a",)], "the key function was called again with other arguments"


async def test_CONTROL_a_working_key_function_still_gives_each_partition_its_own_budget():
    ran: list[str] = []

    @rate_limit(calls=1, window=60.0, key=lambda user_id: user_id)
    async def handler(user_id: str) -> None:
        ran.append(user_id)

    await handler(user_id="a")
    await handler(user_id="b")
    with pytest.raises(Exception, match="rate limit exceeded"):
        await handler(user_id="a")

    assert ran == ["a", "b"]
