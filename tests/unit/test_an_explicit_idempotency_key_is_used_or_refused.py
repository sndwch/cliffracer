"""An explicit idempotency key is the key, or the call is refused.

`@idempotent(key="order.id", hash_payload=True)` reads as "use the order id,
hashed". `hash_payload` modifies the value the key finds; it is not a fallback
for not finding one. A key that does not resolve -- a typo, a renamed field, a
value that is `None` -- raises `IdempotencyKeyError` whether or not
`hash_payload` is set. Hashing every argument instead would key on arguments
the author never named, such as a retry counter, and deduplication would never
fire while looking as if no duplicates had occurred.

A path that resolves to `None` is refused too. `None` is not a key: hashing it
would put every such message under one id, and `str(None)` would do the same.
The message says which of the two happened, because the remedies differ.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from cliffracer.core.exceptions import IdempotencyKeyError
from cliffracer.core.idempotency import IdempotencyContext, compute_payload_hash, idempotent

pytestmark = pytest.mark.unit


@dataclass
class _Order:
    sku: str
    id: str | None = None


@dataclass
class _Envelope:
    order: _Order | None


def _decorate(key: str, hash_payload: bool, *, sync: bool):
    """A handler taking (order, attempt) whose calls record the bound key."""
    seen: list[str | None] = []

    if sync:

        @idempotent(key=key, hash_payload=hash_payload)
        def handler(order, attempt):
            seen.append(IdempotencyContext.get())

        def call(order, attempt):
            handler(order, attempt)

    else:

        @idempotent(key=key, hash_payload=hash_payload)
        async def async_handler(order, attempt):
            seen.append(IdempotencyContext.get())

        def call(order, attempt):
            asyncio.run(async_handler(order, attempt))

    return call, seen


SHAPES = pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
BOTH_MODES = pytest.mark.parametrize("hash_payload", [True, False], ids=["hashed", "raw"])


@SHAPES
@BOTH_MODES
def test_a_key_that_names_a_missing_attribute_is_refused(sync, hash_payload):
    call, seen = _decorate("order.nope", hash_payload, sync=sync)

    with pytest.raises(IdempotencyKeyError, match=r"'order\.nope' not found .*no attribute 'nope'"):
        call(_Order(sku="x"), 1)

    assert seen == [], "the handler ran without a key"


@SHAPES
@BOTH_MODES
def test_a_key_that_names_a_missing_parameter_is_refused(sync, hash_payload):
    call, seen = _decorate("order_id", hash_payload, sync=sync)

    with pytest.raises(IdempotencyKeyError, match=r"'order_id' not found .*not a parameter"):
        call(_Order(sku="x"), 1)

    assert seen == []


@SHAPES
@BOTH_MODES
def test_a_key_that_names_a_missing_dict_entry_is_refused(sync, hash_payload):
    call, seen = _decorate("order.id", hash_payload, sync=sync)

    with pytest.raises(IdempotencyKeyError, match=r"'order\.id' not found .*no key 'id'"):
        call({"sku": "x"}, 1)

    assert seen == []


@SHAPES
@BOTH_MODES
@pytest.mark.parametrize(
    "order",
    [_Order(sku="x", id=None), {"sku": "x", "id": None}],
    ids=["attribute", "dict-entry"],
)
def test_a_key_whose_value_is_none_is_refused_as_none_not_as_missing(sync, hash_payload, order):
    call, seen = _decorate("order.id", hash_payload, sync=sync)

    with pytest.raises(IdempotencyKeyError) as caught:
        call(order, 1)

    message = str(caught.value)
    assert "'order.id' is None" in message, message
    assert "not found" not in message, message
    assert seen == []


@SHAPES
@BOTH_MODES
def test_a_none_part_way_along_the_path_is_named(sync, hash_payload):
    call, seen = _decorate("order.order.id", hash_payload, sync=sync)

    with pytest.raises(
        IdempotencyKeyError, match=r"'order\.order\.id' not found .*'order\.order' is None"
    ):
        call(_Envelope(order=None), 1)

    assert seen == []


@SHAPES
def test_a_resolved_key_with_hash_payload_hashes_that_value_only(sync):
    """The control: the key is used, and arguments it does not name do not move it."""
    call, seen = _decorate("order.id", True, sync=sync)

    call(_Order(sku="x", id="ord-1"), 1)
    call(_Order(sku="y", id="ord-1"), 2)

    assert seen == [compute_payload_hash("ord-1")] * 2


@SHAPES
def test_a_resolved_key_without_hash_payload_is_used_verbatim(sync):
    call, seen = _decorate("order.id", False, sync=sync)

    call({"id": "ord-1"}, 1)

    assert seen == ["ord-1"]


@SHAPES
def test_a_falsy_value_that_is_not_none_is_a_key(sync):
    """Only `None` is refused; the check is not a truthiness test."""
    call, seen = _decorate("order.id", False, sync=sync)

    call({"id": 0}, 1)

    assert seen == ["0"]
