"""A dependency whose timeout could never let its probe run is refused where it is declared.

`asyncio` spends a bound of zero or less before the probe takes a step, so a
dependency declared with `timeout=0` (a mistyped keyword, a unit mix-up, a
config value that arrived as 0) was reported unhealthy forever as "timed out
after 0s", with `/health` answering 503 and nothing in the log saying why. It is
now a `ConfigurationError` naming the dependency, from the decorator, from
`add_dependency` and from `Dependency` itself.

A `Dependency` is also hashable now, and its `detail` is a read-only copy: it
was a frozen dataclass over a dict, so `hash()` raised and the dict it carried
could be changed after the fact.
"""

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.dependencies import Dependency, _run_one, dependency
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit

UNUSABLE = [0, 0.0, -5, -0.5, float("nan"), float("inf"), "2", None, True]
IDS = ["zero", "float-zero", "negative", "float-negative", "nan", "inf", "str", "none", "bool"]


async def _probe() -> None:
    return None


def _service() -> CliffracerService:
    return CliffracerService(ServiceConfig(name="deps_svc"))


@pytest.mark.parametrize("timeout", UNUSABLE, ids=IDS)
def test_the_decorator_refuses_an_unusable_timeout_and_names_the_dependency(timeout):
    with pytest.raises(ConfigurationError) as raised:
        dependency("postgres", timeout=timeout)

    assert "postgres" in str(raised.value)
    assert "timeout" in str(raised.value)


@pytest.mark.parametrize("timeout", UNUSABLE, ids=IDS)
def test_add_dependency_refuses_an_unusable_timeout(timeout):
    svc = _service()

    with pytest.raises(ConfigurationError, match="postgres"):
        svc.add_dependency("postgres", _probe, timeout=timeout)

    assert svc._dependencies == []


@pytest.mark.parametrize("timeout", UNUSABLE, ids=IDS)
def test_a_dependency_value_refuses_an_unusable_timeout(timeout):
    with pytest.raises(ConfigurationError, match="postgres"):
        Dependency(name="postgres", probe=_probe, timeout=timeout)


def test_a_refused_redeclaration_leaves_the_registered_dependency_in_place():
    svc = _service()
    svc.add_dependency("postgres", _probe, timeout=1.0, database="jorbo")
    (original,) = svc._dependencies

    with pytest.raises(ConfigurationError):
        svc.add_dependency("postgres", _probe, timeout=0)

    assert svc._dependencies == [original]
    assert svc.container.registry.dependencies == [original]


def test_a_hand_written_marker_with_an_unusable_timeout_is_refused_at_discovery():
    class Svc(CliffracerService):
        async def check(self):
            return None

    Svc.check._cliffracer_dependency = {"name": "postgres", "timeout": 0, "detail": {}}  # type: ignore[attr-defined]

    with pytest.raises(ConfigurationError, match="postgres"):
        HandlerDiscovery.discover(Svc(ServiceConfig(name="marked")), ServiceConfig(name="marked"))


@pytest.mark.parametrize("timeout", [0.001, 1, 2.5], ids=["tiny", "int", "float"])
def test_CONTROL_a_positive_finite_timeout_is_accepted_and_kept(timeout):
    svc = _service()
    svc.add_dependency("postgres", _probe, timeout=timeout)

    assert svc._dependencies[0].timeout == timeout
    assert dependency("postgres", timeout=timeout) is not None


def test_a_dependency_is_hashable_and_is_its_own_identity():
    a = Dependency(name="x", probe=_probe)
    b = Dependency(name="x", probe=_probe)

    assert hash(a) == hash(a)
    assert len({a, b}) == 2
    assert {a: 1}[a] == 1


def test_a_dependencys_detail_is_a_copy_it_cannot_change():
    source = {"host": "terry"}
    dep = Dependency(name="x", probe=_probe, detail=source)

    source["host"] = "changed"

    assert dep.detail == {"host": "terry"}
    with pytest.raises(TypeError):
        dep.detail["host"] = "again"  # type: ignore[index]


async def test_what_health_reports_is_the_detail_as_declared():
    source = {"host": "terry"}
    dep = Dependency(name="x", probe=_probe, detail=source)
    source["host"] = "changed"

    result = await _run_one(dep)

    assert result["host"] == "terry"
    assert result["ok"] is True
