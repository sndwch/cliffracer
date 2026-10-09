"""Discovery adds the `@dependency` probes to the registry; it does not erase the ones already there.

`add_dependency` writes the probe to both `service._dependencies` and
`service.container.registry.dependencies`. `start()` then runs discovery, which used to REPLACE the
registry's list with the decorator markers alone, so the registry lost every probe added at runtime
while `service._dependencies` (what `/health` reads) kept it: two sources of truth that agree until
the first start and never again. A name that is both decorated and added at runtime is the runtime
one in both places, as it already was for `/health`.
"""

from __future__ import annotations

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.dependencies import dependency

pytestmark = pytest.mark.unit


async def _redis_probe() -> dict:
    return {"ok": True}


async def _runtime_postgres_probe() -> dict:
    return {"ok": True, "from": "runtime"}


class _Declares(CliffracerService):
    @dependency("postgres", timeout=1.0)
    async def _check_postgres(self) -> dict:
        return {"ok": True, "from": "decorator"}


def _names(deps) -> list[str]:
    return [d.name for d in deps]


def _service() -> _Declares:
    return _Declares(ServiceConfig(name="dep_merge_svc", health_port=0))


def test_a_runtime_dependency_is_still_in_the_registry_after_discovery():
    svc = _service()
    svc.add_dependency("redis", _redis_probe)
    assert _names(svc.container.registry.dependencies) == ["postgres", "redis"]

    svc.container.discover_handlers()  # what start() runs

    assert _names(svc.container.registry.dependencies) == ["postgres", "redis"]
    assert _names(svc.container.registry.dependencies) == _names(svc._dependencies), (
        "the registry and the list /health reads have diverged"
    )


def test_a_runtime_probe_replacing_a_decorated_name_stays_the_one_in_the_registry():
    svc = _service()
    svc.add_dependency("postgres", _runtime_postgres_probe)

    svc.container.discover_handlers()

    (postgres,) = [d for d in svc.container.registry.dependencies if d.name == "postgres"]
    assert postgres.probe is _runtime_postgres_probe, "discovery put the decorator's probe back"
    assert postgres is next(d for d in svc._dependencies if d.name == "postgres")


def test_control_discovery_still_finds_the_decorated_probe_once():
    svc = _service()

    svc.container.discover_handlers()

    assert _names(svc.container.registry.dependencies) == ["postgres"]
    assert _names(svc._dependencies) == ["postgres"]


def test_the_merged_list_is_sorted_by_name_whatever_order_it_was_seeded_in():
    from cliffracer.core.dependencies import Dependency
    from cliffracer.core.discovery import HandlerDiscovery
    from cliffracer.core.registry import ServiceRegistry

    svc = _service()
    registry = ServiceRegistry()
    registry.dependencies = [Dependency(name="zeta", probe=_redis_probe)]

    HandlerDiscovery.discover_dependencies(svc, registry)

    assert _names(registry.dependencies) == ["postgres", "zeta"]
