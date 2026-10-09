"""A `TypeAdapter` is built once per annotation, not once per argument per call.

`_encode` was `TypeAdapter(annotation).dump_python(value, mode="json")` and the
generated stubs call it once per argument on every call; `_call` did the same
for the return type. Building the adapter compiles a pydantic-core schema and a
serializer, and the result was discarded immediately. The server already does
the opposite -- `typed_rpc.py` precomputes one adapter per method at discovery
and reuses it at dispatch -- so the client was the only place in the request
path paying for it.

THE TEST COUNTS CONSTRUCTIONS, NOT TIME. A timing assertion on a shared host
measures the host, and would pass or fail for reasons that have nothing to do
with the cache. "How many adapters were built" is the thing the change is
about, and it is exact.

THE CACHE MUST NOT NARROW WHAT CAN BE ENCODED. An annotation carrying
unhashable metadata -- `Annotated[int, ["note"]]` -- is a legal annotation that
`TypeAdapter` accepts today, and a cache keyed on it would raise `TypeError`
instead. That is a regression the cache could introduce, so it has a test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from cliffracer.client import ServiceClient, _adapter_for

pytestmark = pytest.mark.unit


class Item(BaseModel):
    sku: str
    qty: int


class Order(BaseModel):
    lines: list[Item]


def an_order() -> Order:
    return Order(lines=[Item(sku="a", qty=1)])


@pytest.fixture
def built(monkeypatch):
    """Every annotation a `TypeAdapter` was constructed for, cache cleared first."""
    _adapter_for.cache_clear()
    seen: list[object] = []
    real = TypeAdapter

    def counting(annotation, *args, **kwargs):
        seen.append(annotation)
        return real(annotation, *args, **kwargs)

    monkeypatch.setattr("cliffracer.client.TypeAdapter", counting)
    yield seen
    _adapter_for.cache_clear()


def test_one_adapter_serves_every_call_with_the_same_annotation(built):
    """Twenty encodings of the same annotation build one adapter."""
    client = ServiceClient(service="svc", verify=False)

    for _ in range(20):
        client._encode([an_order()], list[Order])

    assert built == [list[Order]], built


def test_CONTROL_two_annotations_get_their_own_adapters(built):
    """Otherwise "built once" could mean "built once, and reused for everything"."""
    client = ServiceClient(service="svc", verify=False)

    client._encode([an_order()], list[Order])
    client._encode(an_order(), Order)
    client._encode("x", str)

    assert built == [list[Order], Order, str], built


def test_two_clients_share_the_adapter_for_one_annotation(built):
    """The cache is keyed on the annotation, so it spans clients and methods."""
    first = ServiceClient(service="a", verify=False)
    second = ServiceClient(service="b", verify=False)

    first._encode(an_order(), Order)
    second._encode(an_order(), Order)

    assert built == [Order], built


async def test_the_return_type_adapter_is_built_once_too(built):
    """`_call` validated the reply through a freshly built adapter every time."""
    client = ServiceClient(service="svc", verify=False)
    client._nc = AsyncMock()
    reply = SimpleNamespace(
        data=json.dumps({"success": True, "result": {"lines": []}}).encode(), headers=None
    )

    with patch.object(ServiceClient, "_request", AsyncMock(return_value=reply)):
        for _ in range(10):
            await client._call("do", {}, Order)

    assert built.count(Order) == 1, built


# --- the cache must not narrow what can be encoded ---------------------------


@pytest.mark.parametrize(
    ("value", "annotation", "expected"),
    [
        (an_order(), Order, {"lines": [{"sku": "a", "qty": 1}]}),
        ([an_order()], list[Order], [{"lines": [{"sku": "a", "qty": 1}]}]),
        (3, Annotated[int, Field(gt=0)], 3),
        (None, Order | None, None),
        (3, Annotated[int, ["unhashable-metadata"]], 3),
    ],
    ids=["model", "list_of_models", "annotated", "optional", "unhashable_annotation"],
)
def test_the_encoded_value_is_unchanged(value, annotation, expected):
    """Including an annotation a cache cannot key on, which must still encode."""
    assert ServiceClient(service="svc", verify=False)._encode(value, annotation) == expected


def test_an_unhashable_annotation_does_not_reach_the_cache(built):
    """It is built every time rather than raising -- correctness over the saving."""
    client = ServiceClient(service="svc", verify=False)
    annotation = Annotated[int, ["unhashable-metadata"]]

    client._encode(1, annotation)
    client._encode(2, annotation)

    assert len(built) == 2, built
