"""The rate-limit marker is cleared after a dispatch, and a key function that names no caller refuses.

`worker_setup` marks a limited handler as already checked, in a context variable, so the
decorator's own standalone check does not count the call twice. `worker_teardown` clears it. Each
RPC runs in a task of its own today, so a marker that were never cleared would die with the task;
anything that dispatches more than one message in one context would instead leave the decorator a
permanent pass-through. And a partition key function that returns `None` (the request lacks the
field) used to be `str()`-ed into the bucket "None", a global limit under a per-caller label.
"""

import pytest
from cliffracer_resilience import (
    RateLimitConfig,
    RateLimitKeyError,
    ResilienceExtension,
    rate_limit,
)
from cliffracer_resilience.rate_limiter import _rate_limit_checked

from cliffracer.core.extension import RejectMessage, WorkerContext

pytestmark = pytest.mark.unit


def _context(payload=None, headers=None) -> WorkerContext:
    return WorkerContext(
        kind="rpc",
        subject="billing.rpc.charge",
        headers=headers or {},
        correlation_id="c-1",
        payload=payload or {},
        data={"handler_name": "charge"},
    )


def _extension_with(config: RateLimitConfig) -> ResilienceExtension:
    extension = ResilienceExtension()
    extension._rate_limits["charge"] = config
    return extension


async def test_the_checked_marker_is_set_by_setup_and_cleared_by_teardown():
    config = RateLimitConfig(calls=5, window=60.0)
    extension = _extension_with(config)
    context = _context()

    await extension.worker_setup(context)
    assert id(config) in _rate_limit_checked.get()

    await extension.worker_teardown(context)
    assert id(config) not in _rate_limit_checked.get()
    assert _rate_limit_checked.get() == frozenset()


async def test_a_second_dispatch_in_the_same_context_is_checked_again_after_teardown():
    """The consequence of a leaked marker: the decorator's own check would be skipped."""
    config = RateLimitConfig(calls=1, window=60.0)
    extension = _extension_with(config)
    first, second = _context(), _context()

    await extension.worker_setup(first)
    await extension.worker_teardown(first)

    with pytest.raises(Exception, match="rate limit exceeded"):
        await extension.worker_setup(second)
    assert _rate_limit_checked.get() == frozenset()


async def test_teardown_of_a_dispatch_that_was_refused_is_harmless():
    extension = _extension_with(RateLimitConfig(calls=1, window=60.0))
    await extension.worker_setup(_context())
    refused = _context()
    with pytest.raises(Exception, match="rate limit exceeded"):
        await extension.worker_setup(refused)

    await extension.worker_teardown(refused)

    assert "_rate_limit_token" not in refused.data


async def test_a_key_function_that_returns_none_is_refused_and_not_a_bucket_called_none():
    config = RateLimitConfig(
        calls=1, window=60.0, key=lambda ctx: ctx.payload.get("tenant"), key_source="context"
    )
    extension = _extension_with(config)

    with pytest.raises(RejectMessage, match="returned None") as refused:
        await extension.worker_setup(_context(payload={}))
    assert refused.value.hook_crash is False
    assert isinstance(refused.value.__cause__, RateLimitKeyError)

    await extension.worker_setup(_context(payload={"tenant": "a"}))


async def test_the_standalone_decorator_refuses_a_key_function_that_returns_none():
    calls = []

    @rate_limit(calls=1, window=60.0, key=lambda tenant=None: tenant)
    async def charge(tenant=None):
        calls.append(tenant)

    with pytest.raises(RateLimitKeyError, match="returned None"):
        await charge()

    await charge(tenant="a")
    assert calls == ["a"]


def test_a_key_function_that_returns_an_empty_string_still_names_a_bucket():
    config = RateLimitConfig(calls=1, window=60.0, key=lambda ctx: "")

    assert config.resolve_key(_context()) == ""
