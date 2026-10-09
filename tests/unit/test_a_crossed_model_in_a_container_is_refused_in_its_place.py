"""A model the typed client would send read as other values is refused wherever the argument holds it.

`ServiceClient._encode` refuses a model argument whose every form the service would read with a
field the caller set holding another value. The same model in a list, a tuple, a dict, an optional,
an `Annotated` or a union where it is an instance of one member class is refused too, named by its
index or key, instead of being sent in the first form the service accepts.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    TypeAdapter,
    field_serializer,
    field_validator,
)

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcValidationError

pytestmark = pytest.mark.unit


class Crossed(BaseModel):
    """Each field's alias is the other's name, and `a`'s validator bumps it: every form the service
    reads back holds the values swapped or bumped twice."""

    a: int = Field(alias="b")
    b: int = Field(alias="a")

    @field_validator("a")
    @classmethod
    def bump(cls, v: int) -> int:
        return v + 1


class Holder(BaseModel):
    items: list[Crossed]
    one: Crossed


class Hidden(BaseModel):
    """`b` is read under "A", `a`'s serialization alias, and by field name the serializer writes `b`
    as `a`: with `a` at its default, `b` is read as its default in every form."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    a: int = Field(0, serialization_alias="A", validation_alias=AliasChoices("A", "a"))
    b: int = Field(0, validation_alias=AliasChoices("A", "b"))

    @field_serializer("b")
    def _b(self, value: int, info: SerializationInfo) -> int:
        return value if info.by_alias else self.a


class Plain(BaseModel):
    n: int = 0


class SubHolder(Holder):
    pass


class Wrapper(BaseModel):
    either: Holder | Plain


class Normalising(BaseModel):
    s: str

    @field_validator("s")
    @classmethod
    def lower(cls, v: str) -> str:
        return v.lower()


CROSSED = Crossed.model_validate({"b": 1, "a": 10})
HOLDER = Holder(items=[CROSSED], one=CROSSED)
FIELDS = ["items", "one.a", "one.b"]


def _encode(value: Any, annotation: Any) -> Any:
    return ServiceClient._encode(None, value, annotation)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "annotation", "at"),
    [
        pytest.param([HOLDER], list[Holder], [0], id="a-list"),
        pytest.param((HOLDER,), tuple[Holder, ...], [0], id="a-tuple"),
        pytest.param((1, HOLDER), tuple[int, Holder], [1], id="a-fixed-tuple"),
        pytest.param({"k": HOLDER}, dict[str, Holder], ["k"], id="a-dict"),
        pytest.param({3: HOLDER}, dict[int, Holder], [3], id="a-dict-with-an-int-key"),
        pytest.param(HOLDER, Holder | None, [], id="an-optional"),
        pytest.param(HOLDER, Annotated[Holder, "doc"], [], id="an-annotated"),
        pytest.param([None, HOLDER], list[Holder | None], [1], id="an-optional-in-a-list"),
        pytest.param([[HOLDER]], list[list[Holder]], [0, 0], id="a-list-in-a-list"),
        pytest.param(HOLDER, Holder | Plain, [], id="a-union-of-two-models"),
        pytest.param(HOLDER, Holder | None | Plain, [], id="a-union-with-none"),
        pytest.param([HOLDER], list[Holder | Plain], [0], id="a-union-in-a-list"),
        pytest.param(HOLDER, Annotated[Holder, "doc"] | Plain, [], id="an-annotated-member"),
    ],
)
def test_a_model_read_as_other_values_is_refused_in_its_place(value, annotation, at):
    with pytest.raises(RpcValidationError) as refused:
        _encode(value, annotation)

    assert [d["loc"] for d in refused.value.details] == [[*at, field] for field in FIELDS]
    named = "Holder" + "".join(f"[{key!r}]" for key in at)
    assert str(refused.value).startswith(f"refused before sending: {named} would arrive"), str(
        refused.value
    )


def test_a_model_read_with_a_field_at_its_default_is_refused_in_its_place():
    with pytest.raises(RpcValidationError) as refused:
        _encode([Hidden(a=0, b=7)], list[Hidden])

    assert [(d["type"], d["loc"]) for d in refused.value.details] == [
        ("value_would_be_lost", [0, "b"])
    ]
    assert str(refused.value).startswith("refused before sending: Hidden[0] would arrive")


def test_CONTROL_a_model_field_holding_a_union_is_refused_by_the_models_own_check():
    """A union in a field, not the argument's annotation, is reached by the model's own check."""
    with pytest.raises(RpcValidationError) as refused:
        _encode(Wrapper(either=HOLDER), Wrapper)

    assert [d["loc"] for d in refused.value.details] == [[f"either.{f}"] for f in FIELDS]


def test_an_instance_of_two_members_of_a_union_is_left_to_the_union():
    """`SubHolder` is a `Holder` too, so which member the service reads it as is the union's to
    decide, and it is sent as the first accepted form, read with its values swapped. Pinned, so a
    change to that decision is a deliberate one."""
    value = SubHolder(items=[CROSSED], one=CROSSED)

    read = TypeAdapter(Holder | SubHolder).validate_python(_encode(value, Holder | SubHolder))

    assert read.one.a != value.one.a


def test_CONTROL_the_bare_model_is_refused_with_no_place_before_its_fields():
    with pytest.raises(RpcValidationError) as refused:
        _encode(HOLDER, Holder)

    assert [d["loc"] for d in refused.value.details] == [[field] for field in FIELDS]
    assert str(refused.value).startswith("refused before sending: Holder would arrive")


@pytest.mark.parametrize(
    ("value", "annotation"),
    [
        pytest.param([Normalising(s="AbC")], list[Normalising], id="a-normalising-model"),
        pytest.param(None, Holder | None, id="none-for-an-optional"),
        pytest.param([], list[Holder], id="an-empty-list"),
        pytest.param(Plain(n=3), Holder | Plain, id="the-other-member-of-a-union"),
    ],
)
def test_CONTROL_a_container_read_as_what_it_holds_is_sent(value, annotation):
    sent = _encode(value, annotation)

    assert TypeAdapter(annotation).validate_python(sent) == value
