"""`OptimizedNATSConnection.subscribe` says it uses the first connection, because it does.

The docstring said "Subscribe using least loaded connection" above a body that always takes the
first, deliberately, so that the messages of different subscriptions keep their order. The
behaviour is pinned by `test_the_connection_pool_rotates_pins_subscriptions_and_cleans_up.py`;
this holds the sentence a reader takes the behaviour from to the same thing.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cliffracer_metrics import OptimizedNATSConnection

pytestmark = pytest.mark.unit


def test_the_docstring_does_not_promise_a_choice_of_connection():
    doc = inspect.getdoc(OptimizedNATSConnection.subscribe) or ""

    assert "least loaded" not in doc.lower()
    assert "first connection" in doc.lower()


async def test_CONTROL_the_code_the_docstring_describes_takes_the_first_connection():
    first = SimpleNamespace(subscribe=AsyncMock(return_value="sub"))
    second = SimpleNamespace(subscribe=AsyncMock(return_value="other"))
    pool = OptimizedNATSConnection(max_connections=2)
    pool._connections = [first, second]

    for _ in range(3):
        await pool.get_connection()
    assert await pool.subscribe("subject.>") == "sub"
    assert first.subscribe.await_count == 1 and second.subscribe.await_count == 0
