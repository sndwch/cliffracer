"""A resilient proxy, and the breaker it carries, belong to one service instance.

`ResilientRpcProxy` caches its per-instance proxy the way `RpcProxy` does, and had the same
fault: two service instances that compared equal shared one proxy and so one breaker, and an
unhashable service could not read the attribute at all.
"""

import pytest
from cliffracer_resilience.circuit_breaker import ResilientRpcProxy

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


class Equal(CliffracerService):
    inventory = ResilientRpcProxy("inventory")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Equal)

    def __hash__(self) -> int:
        return 1


class Unhashable(CliffracerService):
    inventory = ResilientRpcProxy("inventory")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Unhashable)

    __hash__ = None  # type: ignore[assignment]


def _service(cls: type[CliffracerService], name: str) -> CliffracerService:
    return cls(ServiceConfig(name=name, health_port=0))


def test_equal_services_get_their_own_proxy_and_their_own_breaker():
    first, second = _service(Equal, "equal_one"), _service(Equal, "equal_two")

    assert first.inventory is not second.inventory  # type: ignore[attr-defined]
    assert first.inventory.circuit_breaker is not second.inventory.circuit_breaker  # type: ignore[attr-defined]


def test_an_unhashable_service_can_read_the_resilient_proxy():
    svc = _service(Unhashable, "unhashable_resilient")

    assert svc.inventory.circuit_breaker is not None  # type: ignore[attr-defined]


def test_CONTROL_one_instance_keeps_one_proxy_and_one_breaker():
    svc = _service(Equal, "equal_control")

    assert svc.inventory is svc.inventory  # type: ignore[attr-defined]
    assert svc.inventory.circuit_breaker is svc.inventory.circuit_breaker  # type: ignore[attr-defined]
