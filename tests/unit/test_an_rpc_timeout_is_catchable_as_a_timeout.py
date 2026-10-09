"""Wrapping the broker's timeout must not make it harder to catch.

`_request` converts `nats.errors.TimeoutError` into `RpcTimeoutError`. The nats
class is a subclass of the builtin `TimeoutError` -- which in 3.11+ is also
`asyncio.TimeoutError` -- so before the conversion an ordinary
`except TimeoutError:` around a call worked. After it, it did not:

    MRO of RpcTimeoutError:
        RpcTimeoutError -> RpcClientError -> RpcError
        -> cliffracer.core.exceptions.TimeoutError -> ServiceError
        -> CliffracerError -> Exception

with no builtin `TimeoutError` anywhere in it. The wrapper was strictly a
regression in catchability.

WHAT MADE IT INVISIBLE. `cliffracer.core.exceptions` defines its own class
called `TimeoutError`, shadowing the builtin inside that module, and it
inherited only from `ServiceError`. So the line that reads
`class RpcTimeoutError(RpcClientError, TimeoutError)` looks exactly like the fix
for this bug while being the bug. That class is not exported, so in user code
`except TimeoutError:` still binds the builtin and silently stopped matching.

THE FIX IS AT THE SHADOWING CLASS, not at `RpcTimeoutError`. Measured before
choosing: the bare `TimeoutError` is raised nowhere in `src/` or `packages/`
and is exported nowhere, so making it a real builtin `TimeoutError` changes the
behaviour of exactly one thing -- the timeouts that are actually raised -- while
removing the trap rather than working around it.

THE BUILTIN IS AN `OSError`, AND SO WAS THE CLASS THIS REPLACED.
`nats.errors.TimeoutError`'s own MRO is `TimeoutError -> Error -> TimeoutError
-> OSError`, so `except OSError` caught a client timeout before the wrapper too.
This restores the reach that existed, rather than granting new reach.
"""

from __future__ import annotations

import asyncio
import builtins
from unittest.mock import AsyncMock

import nats.errors
import pytest

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import (
    RpcClientError,
    RpcError,
    RpcNoRespondersError,
    RpcTimeoutError,
)
from cliffracer.core.exceptions import TimeoutError as CliffracerTimeoutError

pytestmark = pytest.mark.unit


async def _timeout_from_a_call() -> BaseException:
    """The real `_request` path, with the broker raising its own timeout."""
    client = ServiceClient(service="svc", verify=False)
    client._nc = AsyncMock()
    client._nc.request = AsyncMock(side_effect=nats.errors.TimeoutError())
    try:
        await client._request("svc.rpc.do", b"{}", {})
    except BaseException as exc:  # noqa: BLE001 - the point is which class it is
        return exc
    raise AssertionError("the call did not raise")


async def test_a_client_timeout_is_caught_by_except_timeout_error():
    """The headline: the ordinary spelling works again."""
    raised = await _timeout_from_a_call()

    try:
        raise raised
    except TimeoutError as caught:
        assert isinstance(caught, RpcTimeoutError), type(caught)
    except Exception as caught:  # pragma: no cover - the failure path
        raise AssertionError(
            f"`except TimeoutError` did not catch {type(caught).__name__}"
        ) from caught


async def test_it_is_still_caught_by_except_rpc_error():
    """Nothing is traded away for that."""
    raised = await _timeout_from_a_call()

    try:
        raise raised
    except RpcError as caught:
        assert isinstance(caught, RpcClientError), type(caught)


async def test_asyncio_timeout_error_catches_it_too():
    """`asyncio.TimeoutError` IS the builtin in 3.11+, and callers write both."""
    assert asyncio.TimeoutError is builtins.TimeoutError
    raised = await _timeout_from_a_call()

    try:
        raise raised
    except asyncio.TimeoutError:  # noqa: UP041 - the aliased spelling IS what this asserts
        pass


async def test_the_wrapper_is_catchable_everywhere_the_broker_error_was():
    """The property that was broken, stated directly.

    Every `except` clause that caught what nats raised must still catch what
    the client raises in its place. Asserted by comparing the two against the
    same set rather than by listing the answers.
    """
    wrapped = type(await _timeout_from_a_call())
    original = nats.errors.TimeoutError

    for catcher in (builtins.TimeoutError, asyncio.TimeoutError, OSError, Exception):
        assert issubclass(original, catcher), f"premise: nats' error is not a {catcher.__name__}"
        assert issubclass(wrapped, catcher), (
            f"{wrapped.__name__} is not a {catcher.__name__}, but the "
            f"{original.__name__} it replaced was -- the wrapper narrows what can catch it"
        )


def test_the_shadowing_class_is_a_real_timeout_error():
    """The fix is here, not on the subclass: the trap is the name that lies."""
    assert issubclass(CliffracerTimeoutError, builtins.TimeoutError)


def test_CONTROL_a_non_timeout_client_error_is_not_a_timeout():
    """Otherwise "everything is catchable" would satisfy the tests above."""
    assert not issubclass(RpcNoRespondersError, builtins.TimeoutError)

    with pytest.raises(RpcNoRespondersError):
        try:
            raise RpcNoRespondersError("nothing is subscribed")
        except TimeoutError:  # pragma: no cover - must not be taken
            raise AssertionError("a no-responders error was caught as a timeout") from None


async def test_CONTROL_a_no_responders_error_still_arrives_as_itself():
    """The sibling conversion in the same `except` chain is unchanged."""
    client = ServiceClient(service="svc", verify=False)
    client._nc = AsyncMock()
    client._nc.request = AsyncMock(side_effect=nats.errors.NoRespondersError())

    with pytest.raises(RpcNoRespondersError):
        await client._request("svc.rpc.do", b"{}", {})


def test_the_service_side_timeout_is_the_same_class():
    """`call_rpc` raises the alias; a caller should not need to know which."""
    from cliffracer.core.exceptions import RPCTimeoutError

    assert RPCTimeoutError is RpcTimeoutError
    assert issubclass(RPCTimeoutError, builtins.TimeoutError)
