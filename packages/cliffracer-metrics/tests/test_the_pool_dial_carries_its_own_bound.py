"""The connection pool bounds its own dials, with settings of its own.

ADR-0012's bound belongs to a service's own connection. The pool dials separately: each connection
is bounded by the pool's `connect_timeout` (unset by default, so an unbounded wait) and by its
`max_reconnect_attempts` (10 by default). The decision record states both numbers, so they are read
where they live.
"""

import inspect
from unittest.mock import AsyncMock, patch

import pytest
from cliffracer_metrics import OptimizedNATSConnection

pytestmark = pytest.mark.unit


def test_the_defaults_are_the_numbers_the_decision_record_states():
    parameters = inspect.signature(OptimizedNATSConnection.__init__).parameters

    assert parameters["connect_timeout"].default is None
    assert parameters["max_reconnect_attempts"].default == 10


async def test_a_set_connect_timeout_is_the_bound_each_dial_is_given():
    pool = OptimizedNATSConnection(max_connections=2, connect_timeout=2.5)

    with patch("cliffracer.core.dial.connect", new=AsyncMock()) as dial:
        await pool.connect()

    assert [call.kwargs["timeout"] for call in dial.await_args_list] == [2.5, 2.5]
