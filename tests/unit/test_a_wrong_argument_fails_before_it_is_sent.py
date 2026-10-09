"""`_encode` refuses a value its declared annotation does not accept.

`dump_python` serialises; it does not validate. A wrong-typed argument went on
the wire with only a pydantic serializer warning, and the caller learned about
it a round trip later as `RpcValidationError` attributed to the SERVICE -- for
a mistake that was visible at the call site, in the argument they passed.

The case against checking locally is that the service is the authority, and a
client pinned to an older type could refuse a call a newer service would
accept. Two measurements answer it. Arbitrary unions are unsupported, so a
parameter cannot be widened from `int` to `int | str` at all; and the widenings
that ARE expressible -- a model field changing type, a parameter becoming
optional -- change the method's signature hash, which is what `verify()`
compares. So drift of the kind that would make a local refusal wrong is already
detectable, and already reported as `ClientOutOfDate`.

The value that goes on the wire is the value the caller passed. Validation is a
CHECK here, not a coercion step: its result is discarded, so anything that
passed before is byte-for-byte what it was.
"""

import pytest
from pydantic import BaseModel

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcValidationError

pytestmark = pytest.mark.unit


class Order(BaseModel):
    qty: int


def _encode(value, annotation):
    return ServiceClient._encode(object.__new__(ServiceClient), value, annotation)


def test_a_value_the_annotation_refuses_raises_before_anything_is_sent():
    with pytest.raises(RpcValidationError) as exc:
        _encode("not-an-order", Order)

    assert exc.value.details, "the pydantic errors must survive onto the exception"


def test_the_error_says_the_client_refused_it_rather_than_the_service():
    """`RpcValidationError` already means "the SERVICE rejected the arguments".
    Reusing it keeps one thing for a caller to catch, so the message is what
    tells a reader which side refused."""
    with pytest.raises(RpcValidationError) as exc:
        _encode({"totally": "wrong"}, Order)

    assert "before sending" in str(exc.value), str(exc.value)
    assert "Order" in str(exc.value), str(exc.value)


@pytest.mark.parametrize(
    ("annotation", "spelling"),
    [
        (Order, "Order"),
        (list[Order], "list[Order]"),
        (dict[str, Order], "dict[str, Order]"),
        (Order | None, "Order | None"),
        (list[Order] | None, "list[Order] | None"),
        (int, "int"),
    ],
    ids=["model", "list", "dict", "optional", "optional list", "scalar"],
)
def test_the_error_names_the_declared_type_and_what_was_passed(annotation, spelling):
    """A PARAMETERISED GENERIC HAS A `__name__` AND IT IS THE CONTAINER'S.

    `list[Order].__name__` is `"list"`, so reading that attribute first made a
    bad `list[Order]` argument report "list is not a valid list" -- naming the
    container twice and losing the part that failed. Only a bare model was
    pinned, so nothing caught it. The union spellings are here too because
    `str()` on one keeps the module path that buries the name.
    """
    with pytest.raises(RpcValidationError) as exc:
        _encode("nope", annotation)

    text = str(exc.value)
    assert spelling in text, text
    assert "str is not a valid" in text, text


@pytest.mark.parametrize(
    ("value", "annotation", "expected"),
    [
        (Order(qty=3), Order, {"qty": 3}),
        ([Order(qty=1), Order(qty=2)], list[Order], [{"qty": 1}, {"qty": 2}]),
        (3, int, 3),
        ("s", str, "s"),
        (None, int | None, None),
        ({"k": Order(qty=1)}, dict[str, Order], {"k": {"qty": 1}}),
    ],
    ids=["model", "list of models", "int", "str", "optional none", "dict of models"],
)
def test_CONTROL_a_valid_value_reaches_the_wire_unchanged(value, annotation, expected):
    """The other direction, and the one that would make this change a
    regression. Validation must not become a coercion step."""
    assert _encode(value, annotation) == expected


def test_CONTROL_a_coercible_value_is_unchanged_too():
    """Pydantic would coerce `"3"` to `3` in non-strict mode. The validated
    result is DISCARDED, so what goes on the wire is what went on the wire
    before this change -- otherwise the fix would quietly rewrite payloads."""
    assert _encode("3", int) == "3"


class Line(BaseModel):
    sku: str


class Basket(BaseModel):
    lines: list[Line]


def test_an_instance_of_the_right_class_is_still_the_services_business():
    """The boundary this draws, pinned so it is a decision and not an oversight.

    Pydantic's `revalidate_instances` defaults to `never`, so an object that IS
    a `Basket` is trusted whatever its fields hold. `model_construct` builds
    exactly that, and it still goes to the wire.

    That is the line: THE CLIENT CHECKS THE DECLARED TYPE, THE SERVICE REMAINS
    THE AUTHORITY ON CONTENT. Turning revalidation on here would move content
    authority to the client, which is the half of the objection to local
    validation that has real weight -- a client on an older model would start
    refusing payloads a newer service accepts, and every call would pay to
    revalidate models the caller already built.
    """
    bad = Basket.model_construct(lines="nope")

    assert _encode(bad, Basket) == {"lines": "nope"}


def test_the_declared_type_is_still_checked_for_that_same_model():
    """The control for the test above: the trust is in the INSTANCE, not in the
    model being exempt. Something that is not a `Basket` at all is refused."""
    with pytest.raises(RpcValidationError):
        _encode("not-a-basket", Basket)
