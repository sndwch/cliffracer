"""`CircuitBreakerError` is importable from the package, and a limiter passed in is shared.

Only `RpcCircuitOpenError` was re-exported, so catching the family took an import from the
submodule. And a `RateLimiter` copies itself as itself, so a limiter given to the extension is one
budget across every service built from the declaration, with no `SharedDependency`: that was a
hidden exception to the isolation rule with a test that pinned it and no word of it in the docs.
"""

import cliffracer_resilience
import pytest
from cliffracer_resilience import (
    CircuitBreakerError,
    InMemoryRateLimiter,
    ResilienceExtension,
    RpcCircuitOpenError,
)

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


def test_the_family_is_importable_from_the_package_and_listed():
    assert "CircuitBreakerError" in cliffracer_resilience.__all__
    assert issubclass(RpcCircuitOpenError, CircuitBreakerError)


def test_every_name_in_all_resolves():
    assert [n for n in cliffracer_resilience.__all__ if not hasattr(cliffracer_resilience, n)] == []


def test_catching_the_family_catches_an_open_circuit():
    with pytest.raises(CircuitBreakerError):
        raise RpcCircuitOpenError("the circuit is open")


async def _two_services(declared: ResilienceExtension) -> tuple[CliffracerService, ...]:
    class Svc(CliffracerService):
        resilience = declared

    services = tuple(Svc(ServiceConfig(name=name, health_port=0)) for name in ("a", "b"))
    for service in services:
        await service.container._setup_extensions()
    return services


async def test_a_limiter_passed_in_is_one_limiter_for_every_service_built_from_the_declaration():
    limiter = InMemoryRateLimiter()

    first, second = await _two_services(ResilienceExtension(limiter=limiter))

    assert first.resilience.limiter is limiter and second.resilience.limiter is limiter


async def test_without_a_limiter_each_service_gets_its_own():
    first, second = await _two_services(ResilienceExtension())

    assert first.resilience.limiter is not second.resilience.limiter


async def test_a_limiter_subclass_that_overrides_deepcopy_is_not_shared():
    class PerService(InMemoryRateLimiter):
        def __deepcopy__(self, memo):
            return PerService()

    first, second = await _two_services(ResilienceExtension(limiter=PerService()))

    assert first.resilience.limiter is not second.resilience.limiter
