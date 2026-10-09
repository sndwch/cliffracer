"""A service's proxy to a peer is cached by the instance's identity.

The descriptor lives on the class, so its cache serves every instance. Keyed by the instance
itself, a service that defines `__eq__` without `__hash__` (any `@dataclass` service) raised
`TypeError: unhashable type` on a plain attribute read, and two instances that compared equal
shared one proxy, so the second sent its RPCs through the first. The key is identity now.
"""

import gc
from dataclasses import dataclass

import pytest

from cliffracer import CliffracerService, RpcProxy, ServiceConfig

pytestmark = pytest.mark.unit


class Unhashable(CliffracerService):
    other = RpcProxy("peer")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Unhashable)

    __hash__ = None  # type: ignore[assignment]


class Equal(CliffracerService):
    other = RpcProxy("peer")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Equal)

    def __hash__(self) -> int:
        return 1


class Plain(CliffracerService):
    other = RpcProxy("peer")


def _service(cls: type[CliffracerService], name: str) -> CliffracerService:
    return cls(ServiceConfig(name=name, health_port=0))


def test_an_unhashable_service_can_read_its_proxy():
    svc = _service(Unhashable, "unhashable")

    assert svc.other._service_instance() is svc  # type: ignore[attr-defined]


def test_two_equal_services_each_get_a_proxy_bound_to_themselves():
    first, second = _service(Equal, "equal_one"), _service(Equal, "equal_two")
    assert first == second

    assert first.other is not second.other  # type: ignore[attr-defined]
    assert first.other._service_instance() is first  # type: ignore[attr-defined]
    assert second.other._service_instance() is second  # type: ignore[attr-defined]


def test_CONTROL_one_instance_still_gets_one_proxy():
    svc = _service(Plain, "plain")

    assert svc.other is svc.other  # type: ignore[attr-defined]


def test_the_entry_still_goes_when_an_unhashable_service_does():
    descriptor = Unhashable.__dict__["other"]
    svc = _service(Unhashable, "unhashable_transient")
    _ = svc.other
    assert len(descriptor._proxies) >= 1

    del svc
    gc.collect()

    assert len(descriptor._proxies) == 0


def test_a_dataclass_service_can_read_its_proxy():
    @dataclass
    class Dataclassed(CliffracerService):
        other = RpcProxy("peer")

        def __post_init__(self) -> None:
            super().__init__(ServiceConfig(name="dataclassed", health_port=0))

    svc = Dataclassed()

    assert svc.other._service_instance() is svc
