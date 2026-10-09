"""A call to a service nothing is subscribed for counts against the breaker.

`call_rpc` used to let nats' own no-responders error through, which is neither an `RpcError` nor
anything the breaker monitors, so a dependency that was gone for good never opened its circuit
however many calls failed against it.
"""

from unittest.mock import AsyncMock

import pytest
from cliffracer_resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    RpcCircuitOpenError,
)
from nats.errors import NoRespondersError

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import RpcNoRespondersError

pytestmark = pytest.mark.unit

THRESHOLD = 3


async def test_calls_through_call_rpc_to_a_service_nobody_answers_for_open_the_circuit():
    svc = CliffracerService(ServiceConfig(name="caller"))
    svc.nc = AsyncMock()
    svc.nc.request.side_effect = NoRespondersError()
    breaker = CircuitBreaker("orders", CircuitBreakerConfig(failure_threshold=THRESHOLD))

    for _ in range(THRESHOLD):
        with pytest.raises(RpcNoRespondersError):
            await breaker.call(svc.call_rpc, "orders", "create")

    assert breaker.state == CircuitState.OPEN
    assert breaker.failure_count == THRESHOLD
    sent = svc.nc.request.await_count
    with pytest.raises(RpcCircuitOpenError):
        await breaker.call(svc.call_rpc, "orders", "create")
    assert svc.nc.request.await_count == sent, "an open circuit sent another request"
