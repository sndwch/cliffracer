"""A `@dependency` detail key named `ok`, `error` or `latency_ms` is refused where it is declared.

A dependency's entry in the health payload starts as a copy of its `detail` and is then written
with `ok`, `error` and `latency_ms`. A declared key of one of those names was silently replaced
by the framework's value and its own dropped (`@dependency("db", ok="primary")` reported `ok:
True` and never said "primary"), and `reject_removed_detail` refused only `required=`. The
namespace the framework writes into is shared with the caller's, so a collision is a declaration
mistake, refused the way `required=` and an unusable timeout are.
"""

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.dependencies import Dependency, _run_one, dependency
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit

KEYS = ["ok", "error", "latency_ms"]


async def _probe() -> None:
    return None


@pytest.mark.parametrize("key", KEYS)
def test_the_decorator_refuses_a_key_the_payload_writes_and_names_both(key):
    with pytest.raises(ConfigurationError) as raised:
        dependency("postgres", **{key: "primary"})

    assert "postgres" in str(raised.value) and key in str(raised.value)


@pytest.mark.parametrize("key", KEYS)
def test_add_dependency_refuses_it_and_registers_nothing(key):
    svc = CliffracerService(ServiceConfig(name="deps_svc"))

    with pytest.raises(ConfigurationError, match="postgres"):
        svc.add_dependency("postgres", _probe, **{key: "primary"})

    assert svc._dependencies == []


@pytest.mark.parametrize("key", KEYS)
def test_a_dependency_value_refuses_it(key):
    with pytest.raises(ConfigurationError, match="postgres"):
        Dependency(name="postgres", probe=_probe, detail={key: "primary"})


def test_a_refused_redeclaration_leaves_the_registered_dependency_in_place():
    svc = CliffracerService(ServiceConfig(name="deps_svc"))
    svc.add_dependency("postgres", _probe, host="a")
    (original,) = svc._dependencies

    with pytest.raises(ConfigurationError):
        svc.add_dependency("postgres", _probe, ok="primary")

    assert svc._dependencies == [original]


def test_a_hand_written_marker_with_a_reserved_key_is_refused_at_discovery():
    class Svc(CliffracerService):
        async def check(self):
            return None

    Svc.check._cliffracer_dependency = {  # type: ignore[attr-defined]
        "name": "postgres",
        "timeout": 1.0,
        "detail": {"latency_ms": 5},
    }
    config = ServiceConfig(name="marked")

    with pytest.raises(ConfigurationError, match="postgres"):
        HandlerDiscovery.discover(Svc(config), config)


def test_every_clashing_key_is_named_not_only_the_first():
    with pytest.raises(ConfigurationError) as raised:
        dependency("postgres", ok=1, error="x", latency_ms=2, host="h")

    # The list of keys that clashed, not the fixed sentence that names all three payload keys.
    assert "['ok', 'error', 'latency_ms']" in str(raised.value)
    assert "host" not in str(raised.value)


def test_every_name_the_payload_writes_is_in_the_refused_set():
    """The set is what `_run_one` writes, so a key it starts writing has to be added."""
    from cliffracer.core.dependencies import RESERVED_DETAIL_KEYS

    assert set(RESERVED_DETAIL_KEYS) == {"ok", "error", "latency_ms"}


async def test_CONTROL_other_detail_keys_are_accepted_and_reach_the_payload_beside_the_framework_ones():
    dep = Dependency(name="postgres", probe=_probe, detail={"host": "terry", "database": "jorbo"})

    result = await _run_one(dep)

    assert result["host"] == "terry" and result["database"] == "jorbo"
    assert result["ok"] is True and result["error"] is None and "latency_ms" in result
