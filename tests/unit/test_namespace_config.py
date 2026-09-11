"""ServiceConfig.namespace validation and the _with_namespace helper."""

import pytest

from cliffracer import CliffracerService, ServiceConfig


@pytest.mark.unit
def test_namespace_defaults_none():
    assert ServiceConfig(name="svc").namespace is None


@pytest.mark.unit
def test_valid_namespace_accepted():
    assert ServiceConfig(name="svc", namespace="app1").namespace == "app1"


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["a.b", "a*", "a>", "a b", "", "a.b.c", "a\nb", "a\tb"])
def test_invalid_namespace_rejected(bad):
    with pytest.raises(ValueError):
        ServiceConfig(name="svc", namespace=bad)


@pytest.mark.unit
def test_with_namespace_helper():
    svc = CliffracerService(ServiceConfig(name="svc", namespace="app1"))
    assert svc.container._with_namespace("svc.rpc.foo") == "app1.svc.rpc.foo"
    plain = CliffracerService(ServiceConfig(name="svc"))
    assert plain.container._with_namespace("svc.rpc.foo") == "svc.rpc.foo"
