"""The RpcProxy descriptor, without a broker.

These properties held in a class of broker-free tests that went away with the
file's live-broker conversion: the whole module now skips when no broker is
listening, so nothing checked them at all on a developer machine.
"""

import gc

import pytest

from cliffracer import CliffracerService, RpcProxy, ServiceConfig
from cliffracer.rpc_proxy import ServiceProxy

pytestmark = pytest.mark.unit


class Caller(CliffracerService):
    other = RpcProxy("other_service")


def _caller(name="caller_service"):
    return Caller(ServiceConfig(name=name))


def test_the_descriptor_returns_one_proxy_per_service_instance():
    """A fresh ServiceProxy per attribute access would throw away whatever a
    proxy accumulates and allocate on every call in a hot path."""
    svc = _caller()

    first = svc.other
    assert isinstance(first, ServiceProxy)
    assert svc.other is first, "the proxy is cached against the instance"


def test_two_services_do_not_share_a_proxy():
    a, b = _caller("caller_a"), _caller("caller_b")

    assert a.other is not b.other
    # `_service_instance` and not `.service`: ServiceProxy turns every public
    # attribute into a MethodProxy for a remote call of that name, so reading
    # `.service` would build an RPC proxy for a method called "service".
    assert a.other._service_instance() is a
    assert b.other._service_instance() is b


def test_reading_the_attribute_off_the_class_gives_the_descriptor():
    """`Caller.other` has no instance to proxy for, so it answers with itself
    -- which is what lets the class be introspected without constructing a
    service."""
    assert Caller.other is Caller.__dict__["other"]
    assert isinstance(Caller.other, RpcProxy)


def test_the_proxy_cache_does_not_outlive_the_service():
    """The cache is keyed weakly. A plain dict here would hold every service
    ever constructed alive for the lifetime of the class, and a long-running
    process that builds services per request would grow without bound.
    """
    descriptor = Caller.__dict__["other"]
    svc = _caller("caller_transient")
    _ = svc.other
    assert len(descriptor._proxies) >= 1

    del svc
    gc.collect()

    assert len(descriptor._proxies) == 0, "the entry goes when the service does"
