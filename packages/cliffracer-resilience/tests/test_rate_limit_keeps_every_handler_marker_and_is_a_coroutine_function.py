"""What `@rate_limit` leaves on the function it wraps.

`functools.wraps` copies the function's `__dict__`, so every cliffracer handler marker survives
decoration; the explicit copy of three of them that used to follow was redundant and read as if only
those three were kept. The wrapper is a coroutine function whatever it wraps.
"""

import inspect

import pytest
from cliffracer_resilience import rate_limit

pytestmark = pytest.mark.unit

MARKERS = {
    "_cliffracer_rpc": True,
    "_cliffracer_async_rpc": True,
    "_cliffracer_events": ["orders.created"],
    "_cliffracer_event_durables": {"orders.created": "orders-worker"},
    "_cliffracer_event_fanout": {"orders.created"},
    "_cliffracer_event_cross_namespace": set(),
    "_cliffracer_event_pull": {"orders.created"},
    "_cliffracer_timers": [{"interval": 5}],
    "_cliffracer_idempotent": True,
    "_cliffracer_broadcast": True,
}


def _marked(function):
    for name, value in MARKERS.items():
        setattr(function, name, value)
    return function


def test_every_marker_on_the_handler_is_on_the_wrapper_and_is_the_same_object():
    @rate_limit(calls=5, window=1.0)
    async def handler(self, item: str) -> str:
        return item

    # Decorated the way a handler is: markers first, then the limit above them.
    decorated = rate_limit(calls=5, window=1.0)(_marked(handler.__wrapped__))

    for name, value in MARKERS.items():
        assert getattr(decorated, name) is value, name


def test_the_wrapper_carries_the_limiter_marker_and_not_an_alias_of_it():
    @rate_limit(calls=5, window=1.0)
    async def handler(item: str) -> str:
        return item

    assert handler._cliffracer_rate_limit.calls == 5
    assert not hasattr(handler, "_rate_limit")


def test_the_wrapper_keeps_the_name_and_the_signature():
    @rate_limit(calls=5, window=1.0)
    async def handler(item: str, count: int = 1) -> str:
        """Say the item."""
        return item * count

    assert handler.__name__ == "handler"
    assert handler.__doc__ == "Say the item."
    assert str(inspect.signature(handler)) == "(item: str, count: int = 1) -> str"


async def test_a_synchronous_function_becomes_a_coroutine_function():
    def plain(item: str) -> str:
        return item.upper()

    wrapped = rate_limit(calls=5, window=1.0)(plain)

    assert not inspect.iscoroutinefunction(plain)
    assert inspect.iscoroutinefunction(wrapped)
    coroutine = wrapped(item="a")
    assert inspect.iscoroutine(coroutine)
    assert await coroutine == "A"
