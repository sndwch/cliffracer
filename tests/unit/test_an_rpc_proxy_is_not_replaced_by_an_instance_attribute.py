"""An attribute that is a proxy to a peer cannot be given a value of its own on one instance.

The proxy was a non-data descriptor, so an instance attribute of the same name won: a service
that did `self.inventory = something` in `__init__` silently lost its proxy, and nothing said so.
Assigning to it is refused now, naming the attribute; replacing the proxy on the class still works.
"""

import pytest

from cliffracer import CliffracerService, RpcProxy, ServiceConfig

pytestmark = pytest.mark.unit


class Orders(CliffracerService):
    inventory = RpcProxy("inventory_service")


def _service() -> Orders:
    return Orders(ServiceConfig(name="orders", health_port=0))


def test_assigning_over_a_proxy_is_refused_and_names_the_attribute():
    service = _service()

    with pytest.raises(
        AttributeError, match=r"Orders\.inventory is a proxy to 'inventory_service'"
    ):
        service.inventory = object()  # type: ignore[assignment]

    assert "inventory" not in vars(service)


def test_the_proxy_still_serves_the_instance_after_a_refused_assignment():
    service = _service()
    try:
        service.inventory = object()  # type: ignore[assignment]
    except AttributeError:
        pass

    assert type(service.inventory).__name__ == "ServiceProxy"


def test_CONTROL_a_proxy_is_replaced_on_the_class_and_an_ordinary_attribute_is_assigned(
    monkeypatch,
):
    sentinel = object()
    monkeypatch.setattr(Orders, "inventory", sentinel)
    service = _service()
    service.note = "an ordinary attribute"  # type: ignore[attr-defined]

    assert service.inventory is sentinel
    assert service.note == "an ordinary attribute"  # type: ignore[attr-defined]


def test_a_proxy_built_outside_a_class_body_has_no_attribute_name():
    assert RpcProxy("peer").attr_name is None
