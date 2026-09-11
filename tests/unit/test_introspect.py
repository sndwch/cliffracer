"""Tests verifying describe(cls) introspection behavior over service classes."""

import hashlib
import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer.introspect import Description, canonical, describe


class Order(BaseModel):
    sku: str
    qty: int = 1


ORDER_SCHEMA_HASH = hashlib.sha256(
    json.dumps(Order.model_json_schema(), sort_keys=True).encode()
).hexdigest()[:16]
ORDER_REF = {
    "kind": "model",
    "module": __name__,
    "qualname": "Order",
    "schema_hash": ORDER_SCHEMA_HASH,
}


class Orders(CliffracerService):
    @rpc
    async def create(self, order: Order, note: str = "") -> Order:
        """Create an order."""
        return order

    @rpc
    async def count(self, correlation_id: str | None = None) -> int:
        return 0

    async def not_rpc(self, x: int) -> int:
        return x


@pytest.mark.unit
def test_describe_lists_rpc_methods_sorted_with_params_defaults_and_return():
    d = describe(Orders, service="orders", version="1.0.0")
    assert d.service == "orders" and d.version == "1.0.0"
    assert [m.name for m in d.methods] == ["count", "create"]
    create = d.methods[1]
    assert create.doc == "Create an order."
    assert [p.name for p in create.params] == ["order", "note"]
    assert create.params[0].type == ORDER_REF
    assert create.params[1].default == ""
    assert "default" not in create.params[0].to_dict()
    assert create.returns == ORDER_REF
    assert [p.name for p in d.methods[0].params] == []  # correlation_id excluded


@pytest.mark.unit
def test_hashes_are_stable_and_change_with_the_signature():
    a = describe(Orders, service="orders", version="1.0.0")
    b = describe(Orders, service="orders", version="9.9.9")
    assert a.description_hash == b.description_hash  # version is not part of the hash
    assert a.methods[1].signature_hash.startswith("sha256:")

    class Orders2(Orders):
        @rpc
        async def create(self, order: Order, note: str = "", rush: bool = False) -> Order:  # type: ignore[override]
            return order

    c = describe(Orders2, service="orders", version="1.0.0")
    assert c.methods[1].signature_hash != a.methods[1].signature_hash
    assert c.description_hash != a.description_hash


@pytest.mark.unit
def test_round_trip_through_canonical_json_is_byte_identical():
    d = describe(Orders, service="orders", version="1.0.0")
    text = canonical(d.to_dict())
    again = Description.from_dict(json.loads(text))
    assert canonical(again.to_dict()) == text


@pytest.mark.unit
def test_a_class_with_an_untyped_handler_refuses_like_discovery():
    from cliffracer.core.typed_rpc import UntypedHandler

    class Bad(CliffracerService):
        @rpc
        async def x(self, a):  # noqa: ANN001
            return a

    with pytest.raises(UntypedHandler):
        describe(Bad, service="bad", version="0")


@pytest.mark.unit
def test_describe_includes_components_and_method_docstrings():
    d = describe(Orders, service="orders", version="1.0.0")
    assert ORDER_SCHEMA_HASH in d.components
    assert d.components[ORDER_SCHEMA_HASH]["title"] == "Order"
    create = d.method("create")
    assert create is not None
    assert create.doc == "Create an order."
    assert create.doc_summary == "Create an order."
    assert create.description == "Create an order."
    count = d.method("count")
    assert count is not None
    assert count.doc is None
    assert count.doc_summary is None
    assert count.description is None
