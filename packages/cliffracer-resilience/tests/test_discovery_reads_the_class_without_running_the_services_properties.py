"""Finding the @rate_limit handlers does not evaluate anything on the service.

`_discover_rate_limits` read every attribute of the service with `getattr`, which runs a property.
One that raises (a pool that exists only after `start()`) failed startup with a traceback that
pointed at the resilience extension, and one with a side effect ran once at setup.
"""

import pytest
from cliffracer_resilience import ResilienceExtension, rate_limit

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit


class Orders(CliffracerService):
    resilience = ResilienceExtension()
    evaluated: list[str] = []

    @property
    def pool(self):
        Orders.evaluated.append("pool")
        raise RuntimeError("pool not initialised until start()")

    @property
    def counted(self) -> int:
        Orders.evaluated.append("counted")
        return 1

    @rpc
    @rate_limit(calls=3, window=60.0)
    async def create(self, item: str) -> str:
        return item

    @staticmethod
    @rate_limit(calls=2, window=30.0)
    async def lookup(item: str) -> str:
        return item


async def _setup() -> Orders:
    Orders.evaluated = []
    service = Orders(ServiceConfig(name="orders", health_port=0))
    await service.container._setup_extensions()
    return service


async def test_a_property_that_raises_does_not_fail_startup():
    service = await _setup()

    assert "create" in service.resilience._rate_limits


async def test_no_property_is_evaluated_to_find_the_limits():
    await _setup()

    assert Orders.evaluated == []


async def test_a_method_and_a_staticmethod_are_both_found_with_their_own_limits():
    service = await _setup()
    limits = service.resilience._rate_limits

    assert (limits["create"].calls, limits["create"].window) == (3, 60.0)
    assert (limits["lookup"].calls, limits["lookup"].window) == (2, 30.0)


async def test_an_undecorated_attribute_is_not_a_limit():
    service = await _setup()

    assert set(service.resilience._rate_limits) == {"create", "lookup"}
