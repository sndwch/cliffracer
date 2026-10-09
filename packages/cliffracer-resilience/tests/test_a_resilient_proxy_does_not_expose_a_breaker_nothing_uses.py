"""`ResilientRpcProxy.circuit_breaker` on the class is the breaker calls go through, or it is absent.

Without an explicit `circuit_breaker=`, `__init__` built a breaker that `__get__` never used: each
service instance gets its own, built on first read. Reading `Service.inventory.circuit_breaker`
returned that unused one, so a health endpoint or a test asking "did the circuit open?" always saw
CLOSED with no failures while the real breaker was OPEN. The class now has no breaker of its own
in that case (reading it raises, pointing at the instance), and still returns the shared one when
one was passed in.
"""

from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience import CircuitBreaker, CircuitBreakerConfig, ResilientRpcProxy
from cliffracer_resilience.circuit_breaker import CircuitState, RpcCircuitOpenError

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import RPCTimeoutError

pytestmark = pytest.mark.unit


def _service(cls):
    svc = cls(ServiceConfig(name="orders"))
    svc.call_rpc = AsyncMock(side_effect=RPCTimeoutError("timeout calling inventory"))
    return svc


class _Orders(CliffracerService):
    inventory = ResilientRpcProxy(
        "inventory_service", config=CircuitBreakerConfig(failure_threshold=1, recovery_timeout=60.0)
    )


def test_reading_the_breaker_from_the_class_raises_instead_of_returning_one_nothing_uses():
    with pytest.raises(AttributeError, match="read it from an instance"):
        _ = _Orders.inventory.circuit_breaker

    assert not hasattr(_Orders.inventory, "circuit_breaker")


async def test_the_breaker_the_instance_exposes_is_the_one_the_calls_trip():
    svc = _service(_Orders)
    breaker = svc.inventory.circuit_breaker
    assert breaker.state == CircuitState.CLOSED

    with pytest.raises(RPCTimeoutError):
        await svc.inventory.check_stock(item_id="x")

    assert breaker.state == CircuitState.OPEN
    with pytest.raises(RpcCircuitOpenError):
        await svc.inventory.check_stock(item_id="x")


async def test_CONTROL_with_an_explicit_breaker_the_class_returns_the_shared_one_that_calls_trip():
    shared = CircuitBreaker("shared", CircuitBreakerConfig(failure_threshold=1))

    class Orders(CliffracerService):
        inventory = ResilientRpcProxy("inventory_service", circuit_breaker=shared)

    svc = _service(Orders)

    assert Orders.inventory.circuit_breaker is shared
    assert svc.inventory.circuit_breaker is shared
    with pytest.raises(RPCTimeoutError):
        await svc.inventory.check_stock(item_id="x")
    assert Orders.inventory.circuit_breaker.state == CircuitState.OPEN
