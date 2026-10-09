"""An exception the framework never raises is not part of its public surface.

`ConnectionError` was exported from the package root while nothing raised it, and
its name is a builtin's: `from cliffracer import *` rebound it, so an
`except ConnectionError` in the user's own code stopped catching the socket
errors it was written for. The connection failures a caller handles are
`RpcConnectionError` (a client call that cannot reach the broker) and the
builtin itself. `HandlerError` and `TimerError` were exported and raised by
nothing either.
"""

import builtins
import socket

import pytest

import cliffracer
from cliffracer.core import exceptions
from cliffracer.core.exceptions import CliffracerError, IdempotencyKeyError, ServiceError

pytestmark = pytest.mark.unit

REMOVED = ("ConnectionError", "HandlerError", "TimerError")


@pytest.mark.parametrize("name", REMOVED)
def test_the_exception_is_neither_exported_nor_defined(name):
    assert name not in cliffracer.__all__
    assert not hasattr(cliffracer, name)
    assert not hasattr(exceptions, name)


def test_a_star_import_leaves_the_builtin_connection_error_in_place():
    namespace: dict[str, object] = {}
    exec("from cliffracer import *", namespace)  # noqa: S102

    assert namespace.get("ConnectionError", builtins.ConnectionError) is builtins.ConnectionError


def test_a_refused_socket_is_caught_by_connection_error_after_a_star_import():
    namespace: dict[str, object] = {}
    exec("from cliffracer import *", namespace)  # noqa: S102
    caught = namespace.get("ConnectionError", builtins.ConnectionError)

    with pytest.raises(caught):  # type: ignore[call-overload]
        socket.create_connection(("127.0.0.1", 1), timeout=0.5)


def test_CONTROL_the_idempotency_key_error_is_still_a_service_error():
    """It was a `HandlerError`, which is gone; it must stay where `except
    ServiceError` and `except CliffracerError` still find it."""
    assert issubclass(IdempotencyKeyError, ServiceError)
    assert issubclass(IdempotencyKeyError, CliffracerError)
