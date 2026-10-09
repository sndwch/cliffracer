"""A resilient proxy refuses an instance attribute under its name, as `RpcProxy` does."""

import pytest
from cliffracer_resilience.circuit_breaker import ResilientRpcProxy

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


class Orders(CliffracerService):
    inventory = ResilientRpcProxy("inventory_service")


def test_assigning_over_a_resilient_proxy_is_refused_and_names_the_attribute():
    service = Orders(ServiceConfig(name="orders", health_port=0))

    with pytest.raises(
        AttributeError, match=r"Orders\.inventory is a proxy to 'inventory_service'"
    ):
        service.inventory = object()  # type: ignore[assignment]

    assert type(service.inventory).__name__ == "ResilientServiceProxy"
