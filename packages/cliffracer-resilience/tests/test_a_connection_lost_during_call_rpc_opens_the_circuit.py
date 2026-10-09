"""A connection lost mid-call counts against the circuit, as the standalone client's does.

`call_rpc` let nats' `ConnectionClosedError` out as it came, which is outside the breaker's monitored
set, so a dependency whose connection kept dropping never tripped it. It is an `RpcConnectionError`
now, which the set names.
"""

from unittest.mock import AsyncMock

import nats.errors
import pytest
from cliffracer_resilience import CircuitBreakerConfig, ResilientRpcProxy, RpcCircuitOpenError

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import RpcConnectionError

pytestmark = pytest.mark.unit


class Orders(CliffracerService):
    inventory = ResilientRpcProxy(
        "inventory_service", config=CircuitBreakerConfig(failure_threshold=2)
    )


@pytest.mark.parametrize(
    "lost", [nats.errors.ConnectionClosedError, nats.errors.StaleConnectionError]
)
async def test_calls_that_lose_the_connection_open_the_circuit(lost):
    svc = Orders(ServiceConfig(name="orders", health_port=0))
    svc.nc = AsyncMock()
    svc.nc.request.side_effect = lost()

    for _ in range(2):
        with pytest.raises(RpcConnectionError):
            await svc.inventory.check_stock(item_id="x")

    assert svc.inventory.circuit_breaker.is_open
    with pytest.raises(RpcCircuitOpenError):
        await svc.inventory.check_stock(item_id="x")
    assert svc.nc.request.await_count == 2, "the open circuit did not send the third call"
