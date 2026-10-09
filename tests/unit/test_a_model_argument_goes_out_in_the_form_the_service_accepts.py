"""`ServiceClient._encode` sends the form of a model the service's validation accepts.

The service validates a call with `model_validate` on the model the handler
declares, under that model's own config. The client dumped by field name, so a
model that is only readable by its alias (an `alias` with no `populate_by_name`,
an `alias_generator` without it, such a model nested in another) went out as
`{"item_name": ...}` and was refused as `{"itemName": ...}` was expected.

Dumping by alias always is not the answer: a model with `validate_by_alias=False`
or a `serialization_alias` that differs from its `validation_alias` is accepted
by name and refused by alias, and those calls work today.

So the argument is dumped as before, and only when the annotation does not accept
that dump is the alias form tried. The check is the validation the service runs.
A call that was accepted is byte-for-byte what it was; only a call that was
refused changes.

A form that is accepted but READ differently is the one case worse than a refusal:
two fields whose aliases are each other's names accept the by-name dump and swap
their values. So each try must also read back equal to the argument. When neither
does (a validator or serializer that is not idempotent reads back changed in every
spelling) the first accepted form goes out, as it did.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

import pytest
from pydantic import (
    AliasGenerator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    field_serializer,
    field_validator,
)
from pydantic import ValidationError as PydanticValidationError
from pydantic.alias_generators import to_camel
from pydantic.dataclasses import dataclass

from cliffracer.client import RpcValidationError, ServiceClient
from cliffracer.core.validation import wire_models

pytestmark = pytest.mark.unit


class AliasOnly(BaseModel):
    item_name: str = Field(alias="itemName")


class Populatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    item_name: str = Field(alias="itemName")


class ByNameOnly(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    item_name: str = Field(alias="itemName")


class SplitAliases(BaseModel):
    item_name: str = Field(validation_alias="item_in", serialization_alias="itemOut")
    model_config = ConfigDict(validate_by_name=True)


class Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)
    item_name: str


class CamelPopulatable(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    item_name: str


class SerializedByAlias(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True, populate_by_name=True)
    item_name: str = Field(alias="itemName")


class Plain(BaseModel):
    """No alias, and a field whose JSON form is not its python value."""

    item_name: str
    when: datetime


@dataclass(config=ConfigDict(populate_by_name=True))
class PopulatableDataclass:
    """Not a `BaseModel`, and its alias dump is accepted as well as its plain one."""

    item_name: str = Field(alias="itemName")


class Inner(BaseModel):
    inner_name: str = Field(alias="innerName")


class Outer(BaseModel):
    outer_name: str = Field(alias="outerName")
    inner: Inner


class CamelOuter(BaseModel):
    model_config = ConfigDict(alias_generator=AliasGenerator(validation_alias=to_camel))
    inner: Camel


class Stamped(BaseModel):
    """Its validator cannot read the JSON form of its own field: `TypeError`, not a `ValidationError`."""

    when: datetime

    @field_validator("when", mode="before")
    @classmethod
    def _wants_a_datetime(cls, value):
        return value.replace(microsecond=0)


class StampedAliased(BaseModel):
    """`Stamped` with an alias, so the plain dump and the alias dump are different forms."""

    when: datetime = Field(alias="When")

    @field_validator("when", mode="before")
    @classmethod
    def _wants_a_datetime(cls, value):
        return value.replace(microsecond=0)


class Swapped(BaseModel):
    """Each field's alias is the other's field name: the by-name dump is accepted and read swapped."""

    a: str = Field(alias="b")
    b: str = Field(alias="a")


class Bumped(BaseModel):
    """A validator that is not idempotent: a model read back from its own dump has changed."""

    model_config = ConfigDict(populate_by_name=True)
    n: int = Field(alias="nN")

    @field_validator("n")
    @classmethod
    def _plus_one(cls, value: int) -> int:
        return value + 1


class BumpedAliasOnly(BaseModel):
    n: int = Field(alias="nN")

    @field_validator("n")
    @classmethod
    def _plus_one(cls, value: int) -> int:
        return value + 1


class SwappedBumped(BaseModel):
    """Each field's alias is the other's name, and `a` is bumped on every read: no form reads back
    equal, the plain dump is accepted and read crossed, and the alias dump is read faithfully."""

    a: int = Field(alias="b")
    b: int = Field(alias="a")

    @field_validator("a")
    @classmethod
    def _plus_one(cls, value: int) -> int:
        return value + 1


class ByNameWithAToken(BaseModel):
    """Read only by name and dumped by alias; every read builds a new token, so the by-name form is
    accepted and read as other values, and no form is read faithfully."""

    model_config = ConfigDict(
        validate_by_alias=False, validate_by_name=True, serialize_by_alias=True
    )
    n: int = Field(alias="nN")
    token: str = Field(default_factory=lambda: uuid.uuid4().hex, exclude=True)


class Doubled(BaseModel):
    """A serializer that is not idempotent."""

    model_config = ConfigDict(populate_by_name=True)
    n: Annotated[int, PlainSerializer(lambda value: value * 2)] = Field(alias="nN")


class WithExcludedField(BaseModel):
    item: str = Field(alias="itemName")
    secret: str = Field(default="s", exclude=True)


class NeverEqual(BaseModel):
    """Compares by raising: an equality that cannot be answered is "not equal"."""

    item: str = Field(alias="itemName")

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("no answer")

    __hash__ = None  # type: ignore[assignment]


class SwappedPopulatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    a: str = Field(alias="b")
    b: str = Field(alias="a")


class Chained(BaseModel):
    """`a` is read from "b", `b` from "c", and `c` by its name, which is also `b`'s alias."""

    a: str = Field(alias="b")
    b: str = Field(alias="c")
    c: str


class ChainedWithDefaults(BaseModel):
    """`Chained` whose `a` has a default, so a value read in its place could be taken for it."""

    a: str = Field("1", alias="b")
    b: str = Field(alias="c")
    c: str


class Aliased(BaseModel):
    item_name: str = Field(alias="itemName")


class Plainly(BaseModel):
    item_name: str


class Boxed(BaseModel):
    """The by-name dump of an `Aliased` here is read as a `Plainly`: the class switches."""

    x: Aliased | Plainly


class ByNameOuter(BaseModel):
    """Read only by field name, dumped by alias, around an alias-only `Inner`."""

    model_config = ConfigDict(
        validate_by_alias=False, validate_by_name=True, serialize_by_alias=True
    )
    outer_name: str = Field(alias="outerName")
    inner: Inner


class AliasOnlySerialized(BaseModel):
    """Alias-only, with a serializer: only the whole-value alias dump is read back."""

    item_name: str = Field(alias="itemName")

    @field_serializer("item_name")
    def _same(self, value: str) -> str:
        return value


class ByNameSerialized(BaseModel):
    """Read only by name and dumped by alias, with a serializer: only the by-name dump is read."""

    model_config = ConfigDict(
        validate_by_alias=False, validate_by_name=True, serialize_by_alias=True
    )
    item_name: str = Field(alias="itemName")

    @field_serializer("item_name")
    def _same(self, value: str) -> str:
        return value


class PopulatableDumpedByAlias(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    item_name: str = Field(alias="itemName")


class ByNameOverPopulatable(BaseModel):
    """The whole by-name dump serves this tree before any per-level form is tried."""

    model_config = ConfigDict(
        validate_by_alias=False, validate_by_name=True, serialize_by_alias=True
    )
    outer_name: str = Field(alias="outerName")
    both: PopulatableDumpedByAlias


class ByNameDumpedByAlias(BaseModel):
    model_config = ConfigDict(
        validate_by_alias=False, validate_by_name=True, serialize_by_alias=True
    )
    item_name: str = Field(alias="itemName")


class ByNameBumped(BaseModel):
    """Read only by name, and its validator adds one on every read."""

    model_config = ConfigDict(
        validate_by_alias=False, validate_by_name=True, serialize_by_alias=True
    )
    n: int = Field(alias="nN")

    @field_validator("n")
    @classmethod
    def _plus_one(cls, value: int) -> int:
        return value + 1

    @field_serializer("n")
    def _same(self, value: int) -> int:
        return value


def _chained(a: str, b: str, c: str) -> Chained:
    """A `Chained` holding exactly these three field values. Its aliases collide with its names,
    so the constructor cannot say them; `model_construct` reads `a` through the alias `b`."""
    value = Chained.model_construct(**{"b": a, "c": b})
    value.c = c
    return value


def _encode(value, annotation):
    return ServiceClient._encode(object.__new__(ServiceClient), value, annotation)


def _main_encoding(value, annotation):
    """What `_encode` returned before it chose a form: the plain JSON-mode dump."""
    return TypeAdapter(annotation).dump_python(value, mode="json")


@pytest.mark.parametrize(
    ("value", "annotation", "wire"),
    [
        pytest.param(AliasOnly(itemName="x"), AliasOnly, {"itemName": "x"}, id="alias-only"),
        pytest.param(Camel(itemName="x"), Camel, {"itemName": "x"}, id="alias-generator"),
        pytest.param(
            Outer(outerName="o", inner=Inner(innerName="i")),
            Outer,
            {"outerName": "o", "inner": {"innerName": "i"}},
            id="nested",
        ),
        pytest.param([AliasOnly(itemName="x")], list[AliasOnly], [{"itemName": "x"}], id="list"),
        pytest.param(
            {"k": AliasOnly(itemName="x")},
            dict[str, AliasOnly],
            {"k": {"itemName": "x"}},
            id="dict",
        ),
        pytest.param(AliasOnly(itemName="x"), AliasOnly | None, {"itemName": "x"}, id="optional"),
        pytest.param(
            CamelOuter(inner=Camel(itemName="x")),
            CamelOuter,
            {"inner": {"itemName": "x"}},
            id="generator-nested",
        ),
        pytest.param(
            ByNameOuter(outer_name="o", inner=Inner(innerName="i")),
            ByNameOuter,
            {"outer_name": "o", "inner": {"innerName": "i"}},
            id="by-name-outer",
        ),
        pytest.param(
            ByNameOuter(outer_name="o", inner=Inner(innerName="i")),
            ByNameOuter | None,
            {"outer_name": "o", "inner": {"innerName": "i"}},
            id="by-name-outer-optional",
        ),
        pytest.param(
            [ByNameOuter(outer_name="o", inner=Inner(innerName="i"))],
            list[ByNameOuter],
            [{"outer_name": "o", "inner": {"innerName": "i"}}],
            id="by-name-outer-list",
        ),
    ],
)
def test_a_model_only_readable_by_its_alias_is_sent_by_its_alias(value, annotation, wire):
    sent = _encode(value, annotation)

    assert sent == wire
    # The point of the form: the service's own validation takes what was sent, and reads it as
    # the argument that was passed.
    assert TypeAdapter(annotation).validate_python(sent) == value


@pytest.mark.parametrize(
    ("value", "annotation", "wire"),
    [
        pytest.param(Populatable(itemName="x"), Populatable, {"item_name": "x"}, id="populatable"),
        pytest.param(
            ByNameOnly(item_name="x"), ByNameOnly, {"item_name": "x"}, id="validate_by_alias-off"
        ),
        pytest.param(
            SplitAliases(item_name="x"), SplitAliases, {"item_name": "x"}, id="split-aliases"
        ),
        pytest.param(
            CamelPopulatable(itemName="x"),
            CamelPopulatable,
            {"item_name": "x"},
            id="generator-populatable",
        ),
        pytest.param(
            SerializedByAlias(itemName="x"),
            SerializedByAlias,
            {"itemName": "x"},
            id="serialize_by_alias",
        ),
        pytest.param(
            Plain(item_name="x", when=datetime(2026, 1, 1)),
            Plain,
            {"item_name": "x", "when": "2026-01-01T00:00:00"},
            id="no-alias",
        ),
        pytest.param(
            [Populatable(itemName="x")], list[Populatable], [{"item_name": "x"}], id="list"
        ),
    ],
)
def test_a_call_that_was_accepted_is_sent_exactly_as_before(value, annotation, wire):
    """The expected dicts are written out, and also equal the plain dump: the pin is
    "what main sent", which `by_alias=True` changes for `Populatable` and breaks for
    `ByNameOnly` and `SplitAliases`."""
    sent = _encode(value, annotation)

    assert sent == wire
    assert sent == _main_encoding(value, annotation)
    assert TypeAdapter(annotation).validate_python(sent) == value


def test_a_model_serialized_by_alias_keeps_the_alias_its_config_asks_for():
    """The first dump is the unchanged call, so the model's own `serialize_by_alias` decides
    it. A first try that forced `by_alias=False` sends `item_name` here."""
    assert _encode(SerializedByAlias(itemName="x"), SerializedByAlias) == {"itemName": "x"}


@pytest.mark.parametrize("annotation", [Stamped, StampedAliased], ids=["no-alias", "an-alias"])
def test_a_validator_that_cannot_read_the_dump_does_not_stop_the_call(annotation):
    """The check is a probe of whether the service would accept a form. A validator that
    raises something other than `ValidationError` on the JSON form is "not accepted",
    and the call goes out as it did, in the plain dump, the first form made; it does not fail
    on the caller's side."""
    value = annotation.model_validate({"when": datetime(2026, 1, 1, 12, 0, 0, 5)}, by_name=True)

    sent = _encode(value, annotation)

    assert sent == _main_encoding(value, annotation) == {"when": "2026-01-01T12:00:00"}


@pytest.mark.parametrize(
    ("value", "annotation", "wire"),
    [
        pytest.param(
            uuid.UUID(int=1), uuid.UUID, "00000000-0000-0000-0000-000000000001", id="uuid"
        ),
        pytest.param(
            PopulatableDataclass(item_name="x"),
            PopulatableDataclass,
            {"item_name": "x"},
            id="populatable-dataclass",
        ),
    ],
)
def test_a_value_that_is_not_a_model_is_sent_as_before(value, annotation, wire):
    """A `UUID` dumps to text only in JSON mode, and the dataclass accepts both its alias
    dump and its plain one, so these two pin which form goes out: the plain JSON-mode dump."""
    sent = _encode(value, annotation)

    assert sent == wire
    assert sent == _main_encoding(value, annotation)


@pytest.mark.parametrize(
    ("wire", "annotation"),
    [
        pytest.param({"itemName": "x"}, AliasOnly, id="alias-only"),
        pytest.param({"item_name": "x"}, Populatable, id="populatable"),
    ],
)
def test_a_dict_the_caller_built_in_wire_form_is_not_rewritten(wire, annotation):
    """A dict that the annotation accepts is already what goes on the wire. It is dumped
    as it was before, by neither form."""
    assert _encode(wire, annotation) == wire


def test_a_form_accepted_but_read_swapped_is_not_the_one_sent():
    """The by-name dump of `Swapped` is accepted (its keys are the other field's alias) and
    read with the two values exchanged. The alias form reads back as the argument."""
    value = Swapped(b="1", a="2")
    assert (value.a, value.b) == ("1", "2")
    assert _main_encoding(value, Swapped) == {"a": "1", "b": "2"}

    sent = _encode(value, Swapped)

    assert sent == {"b": "1", "a": "2"}
    assert TypeAdapter(Swapped).validate_python(sent) == value


@pytest.mark.parametrize(
    ("value", "annotation"),
    [
        pytest.param(Bumped(nN=4), Bumped, id="validator"),
        pytest.param(Doubled(nN=5), Doubled, id="serializer"),
    ],
)
def test_a_model_that_reads_back_changed_in_every_form_is_sent_as_before(value, annotation):
    """A validator or serializer that is not idempotent fails the read-back in both spellings
    (here neither equals the argument, and both are accepted). The call goes out in the first
    accepted form, the plain dump, exactly as it did."""
    sent = _encode(value, annotation)

    assert sent == _main_encoding(value, annotation)
    assert TypeAdapter(annotation).validate_python(sent) != value


@pytest.mark.parametrize(
    ("value", "annotation", "wire"),
    [
        pytest.param(BumpedAliasOnly(nN=4), BumpedAliasOnly, {"nN": 5}, id="validator"),
        pytest.param(ByNameBumped(n=4), ByNameBumped, {"n": 5}, id="by-name-validator"),
        pytest.param(
            [ByNameBumped(n=4)], list[ByNameBumped], [{"n": 5}], id="by-name-validator-in-a-list"
        ),
        pytest.param(
            {"k": ByNameBumped(n=4)},
            dict[str, ByNameBumped],
            {"k": {"n": 5}},
            id="by-name-validator-in-a-dict",
        ),
        pytest.param(
            ByNameBumped(n=4), ByNameBumped | None, {"n": 5}, id="by-name-validator-optional"
        ),
        pytest.param(
            (ByNameBumped(n=4), ByNameBumped(n=7)),
            tuple[ByNameBumped, ...],
            [{"n": 5}, {"n": 8}],
            id="by-name-validator-in-a-tuple",
        ),
        pytest.param(
            WithExcludedField(itemName="x", secret="t"),
            WithExcludedField,
            {"itemName": "x"},
            id="excluded-field",
        ),
    ],
)
def test_an_alias_only_model_that_never_reads_back_equal_is_still_sent_in_the_form_accepted(
    value, annotation, wire
):
    """Equality is a preference between accepted forms, not a condition of sending one: with
    the plain dump refused and the alias dump accepted but not equal, the alias dump goes."""
    sent = _encode(value, annotation)

    assert sent == wire
    TypeAdapter(annotation).validate_python(sent)


# (hold the model, annotate its class, place what is held) -- a tuple is held as a tuple and sent
# as a JSON list, so it is placed differently on the wire and as read.
_IN_ITS_PLACE = [
    pytest.param(lambda m: m, lambda t: t, lambda w: w, lambda r: r, id="bare"),
    pytest.param(lambda m: [m], lambda t: list[t], lambda w: [w], lambda r: [r], id="in-a-list"),
    pytest.param(
        lambda m: (m,), lambda t: tuple[t, ...], lambda w: [w], lambda r: (r,), id="in-a-tuple"
    ),
    pytest.param(
        lambda m: {"k": m},
        lambda t: dict[str, t],
        lambda w: {"k": w},
        lambda r: {"k": r},
        id="in-a-dict",
    ),
    pytest.param(lambda m: m, lambda t: t | None, lambda w: w, lambda r: r, id="optional"),
    pytest.param(
        lambda m: [[m]],
        lambda t: list[list[t]],
        lambda w: [[w]],
        lambda r: [[r]],
        id="list-of-lists",
    ),
    pytest.param(
        lambda m: {"k": [m]},
        lambda t: dict[str, list[t]],
        lambda w: {"k": [w]},
        lambda r: {"k": [r]},
        id="dict-of-lists",
    ),
    pytest.param(
        lambda m: [m], lambda t: list[t] | None, lambda w: [w], lambda r: [r], id="optional-list"
    ),
]


def _models_in(read):
    """The models a read value holds, in order, at any depth of lists, tuples and dict values."""
    if isinstance(read, BaseModel):
        return [read]
    members = read.values() if isinstance(read, dict) else read
    return [model for member in members for model in _models_in(member)]


@pytest.mark.parametrize(("hold", "annotate", "on_the_wire", "as_read"), _IN_ITS_PLACE)
def test_a_model_held_in_a_container_is_read_as_its_validators_make_it_not_crossed(
    hold, annotate, on_the_wire, as_read
):
    """The plain dump is accepted and read with `a` and `b` crossed. Each model is tested in its
    place as a bare one is, so the alias dump goes, and the service reads `a` as its own
    validator makes of the caller's `a`, and `b` as `b`."""
    value = SwappedBumped.model_validate({"b": 1, "a": 10})
    assert (value.a, value.b) == (2, 10)
    annotation = annotate(SwappedBumped)

    sent = _encode(hold(value), annotation)

    read = TypeAdapter(annotation).validate_python(sent)
    assert [(m.a, m.b) for m in _models_in(read)] == [(3, 10)]
    assert read == as_read(SwappedBumped.model_validate({"b": 2, "a": 10}))
    assert sent == on_the_wire({"b": 2, "a": 10})


class Unrelated(BaseModel):
    """A second member for a union: it cannot read `SwappedBumped`'s forms."""

    z: int


class SwappedTwin(BaseModel):
    """A second member that reads `SwappedBumped`'s forms too, but bumps nothing: a union of the two
    is read faithfully when either member reads it so, not only when both do."""

    a: int = Field(alias="b")
    b: int = Field(alias="a")


def _every_model_in(read):
    """The models `read` holds at any depth, in order."""
    if isinstance(read, BaseModel):
        return [read]
    items = read.values() if isinstance(read, dict) else read
    return [m for item in items for m in _every_model_in(item)]


@pytest.mark.parametrize(
    ("hold", "annotate", "models"),
    [
        pytest.param(lambda m: (m, m), lambda t: tuple[t, t], 2, id="fixed-tuple"),
        pytest.param(lambda m: m, lambda t: Annotated[t, "x"], 1, id="annotated"),
        pytest.param(lambda m: m, lambda t: t | Unrelated, 1, id="union"),
        pytest.param(lambda m: m, lambda t: t | SwappedTwin, 1, id="union-of-two-readers"),
        pytest.param(lambda m: [m], lambda t: list[t | Unrelated], 1, id="list-of-a-union"),
        pytest.param(lambda m: [[m]], lambda t: list[list[t]], 1, id="list-of-lists"),
        pytest.param(lambda m: {"k": [m]}, lambda t: dict[str, list[t]], 1, id="dict-of-lists"),
    ],
)
def test_a_model_in_any_held_shape_is_read_as_its_validators_make_it_not_crossed(
    hold, annotate, models
):
    """What the SERVICE reads, through the whole annotation (a union as a union, which picks its
    member by what validates): each model a `SwappedBumped`, with `a` as its validator makes of the
    caller's `a`, and `b` as `b`. The plain dump is accepted and read crossed, `a=11 b=2`; a twin
    reading the sent form would hold `a=2 b=10`."""
    value = SwappedBumped.model_validate({"b": 1, "a": 10})
    annotation = annotate(SwappedBumped)

    sent = _encode(hold(value), annotation)

    read = TypeAdapter(annotation).validate_python(sent)
    held = _every_model_in(read)
    assert [(type(m), m.a, m.b) for m in held] == [(SwappedBumped, 3, 10)] * models


@pytest.mark.parametrize(("hold", "annotate", "on_the_wire", "as_read"), _IN_ITS_PLACE)
def test_a_form_read_as_other_values_does_not_replace_a_refused_one_in_a_container(
    hold, annotate, on_the_wire, as_read
):
    """The by-name form is accepted but read with a new token, so it is not the argument; in a
    container as for a bare model, the plain dump goes and the service refuses it."""
    value = ByNameWithAToken(n=4)

    assert _encode(hold(value), annotate(ByNameWithAToken)) == on_the_wire({"nN": 4})


@pytest.mark.parametrize(
    ("value", "annotation", "wire"),
    [
        pytest.param(NeverEqual(itemName="x"), NeverEqual, {"itemName": "x"}, id="direct"),
        pytest.param(NeverEqual(itemName="x"), NeverEqual | None, {"itemName": "x"}, id="optional"),
        pytest.param([NeverEqual(itemName="x")], list[NeverEqual], [{"itemName": "x"}], id="list"),
    ],
)
def test_an_equality_that_raises_does_not_stop_the_call(value, annotation, wire):
    """Under `| None` and in a list the read-back is compared by equality first, an equality that
    raises counting as "not equal", and then each model in its place field by field."""
    assert _encode(value, annotation) == wire


@pytest.mark.parametrize(
    ("value", "annotation", "wire"),
    [
        pytest.param(
            SwappedPopulatable(b="1", a="2"),
            SwappedPopulatable,
            {"b": "1", "a": "2"},
            id="swapped-populatable",
        ),
        pytest.param(
            _chained("A", "B", "B"),
            Chained,
            {"b": "A", "c": "B"},
            id="chain-with-two-equal-values",
        ),
        pytest.param(
            Boxed(x=Aliased(itemName="p")),
            Boxed,
            {"x": {"itemName": "p"}},
            id="union-member-switches-class",
        ),
    ],
)
def test_other_forms_accepted_but_read_as_something_else_are_not_sent(value, annotation, wire):
    """The comparison is of the validated OBJECT with the argument: the two members of the union
    dump alike, so a comparison of dumps would not see the service build the other class."""
    adapter = TypeAdapter(annotation)
    assert adapter.validate_python(_main_encoding(value, annotation)) != value

    sent = _encode(value, annotation)

    assert sent == wire
    assert adapter.validate_python(sent) == value


def test_a_chain_of_aliases_with_distinct_values_has_no_form_that_reads_back_and_is_refused():
    """`b`'s alias is `c`'s name, so a dict cannot hold both values under one key: the alias
    form loses one, and the plain form is read shifted. Neither reads back as the argument, and
    the plain dump would deliver `a` holding `b`'s value and `b` holding `c`'s, so the call is
    refused before sending, naming both."""
    value = _chained("1", "2", "3")
    adapter = TypeAdapter(Chained)
    aliased = adapter.dump_python(value, mode="json", by_alias=True)
    assert aliased == {"b": "1", "c": "3"}
    assert adapter.validate_python(aliased) != value
    assert adapter.validate_python(_main_encoding(value, Chained)) == _chained("2", "3", "3")

    with pytest.raises(RpcValidationError) as refused:
        _encode(value, Chained)

    assert {(d["type"], d["loc"][0]) for d in refused.value.details} == {
        ("value_would_be_misread", "a"),
        ("value_would_be_misread", "b"),
    }


def test_a_defaulted_field_read_as_another_fields_value_is_named_misread_not_lost():
    """`a`'s default is "1" and it would arrive holding `b`'s "2": that is a misread value, and
    the refusal names whose value it holds rather than calling it read as its default."""
    value = ChainedWithDefaults.model_construct(**{"b": "1", "c": "2"})
    value.c = "3"

    with pytest.raises(RpcValidationError) as refused:
        _encode(value, ChainedWithDefaults)

    details = {d["loc"][0]: d for d in refused.value.details}
    assert {(d["type"], loc) for loc, d in details.items()} == {
        ("value_would_be_misread", "a"),
        ("value_would_be_misread", "b"),
    }
    assert details["a"]["msg"].endswith("it would arrive holding b's value")


@pytest.mark.parametrize(
    ("value", "annotation", "wire"),
    [
        pytest.param(
            AliasOnlySerialized(itemName="x"),
            AliasOnlySerialized,
            {"itemName": "x"},
            id="alias-dump-only",
        ),
        pytest.param(
            ByNameSerialized(item_name="x"),
            ByNameSerialized,
            {"item_name": "x"},
            id="by-name-dump-only",
        ),
        pytest.param(
            ByNameOverPopulatable(outer_name="o", both=PopulatableDumpedByAlias(item_name="b")),
            ByNameOverPopulatable,
            {"outer_name": "o", "both": {"item_name": "b"}},
            id="by-name-whole-dump-over-a-per-level-form",
        ),
    ],
)
def test_a_value_a_whole_value_dump_serves_is_sent_in_that_dump(value, annotation, wire):
    """Both whole-value dumps, by alias and then by field name, are offered before the per-level
    form, so the value goes out in the one of them it reads back from."""
    sent = _encode(value, annotation)

    assert sent == wire
    TypeAdapter(annotation).validate_python(sent)


@pytest.mark.parametrize(
    ("value", "annotation", "wire"),
    [
        pytest.param(
            ByNameDumpedByAlias(item_name="x"), ByNameDumpedByAlias, {"item_name": "x"}, id="direct"
        ),
        pytest.param(
            ByNameDumpedByAlias(item_name="x"),
            ByNameDumpedByAlias | None,
            {"item_name": "x"},
            id="optional",
        ),
        pytest.param(
            [ByNameDumpedByAlias(item_name="x")],
            list[ByNameDumpedByAlias],
            [{"item_name": "x"}],
            id="list",
        ),
    ],
)
def test_a_model_read_only_by_name_but_dumped_by_alias_is_sent_by_name(value, annotation, wire):
    """Its first dump, by alias, is not read back, so the client chooses again: by the model's
    own read-back for a model annotation, by equality under `| None` or in a list."""
    sent = _encode(value, annotation)

    assert sent == wire
    assert TypeAdapter(annotation).validate_python(sent) == value


class SubOfByNameDumpedByAlias(ByNameDumpedByAlias):
    """Adds nothing: an instance of it is sent where `ByNameDumpedByAlias` is declared."""


def test_a_subclass_argument_is_judged_by_the_fields_its_declared_model_reads():
    """A model annotation reads the argument field by field, so a subclass instance, never equal to
    what the declared class builds, is still found read back and sent by name."""
    value = SubOfByNameDumpedByAlias(item_name="x")

    sent = _encode(value, ByNameDumpedByAlias)

    assert sent == {"item_name": "x"}
    assert TypeAdapter(ByNameDumpedByAlias).validate_python(sent).item_name == "x"


class _Priced(BaseModel):
    price: Decimal


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("path", ["generated client", "call_rpc"])
def test_a_value_the_declared_class_refuses_is_sent_and_refused_by_the_service(text, path):
    """A `Decimal` NaN or infinity can only be held by bypassing validation (`model_construct`): the
    class's own validator refuses it. The client does not refuse it first, since the receiver may
    be another version of the class; it goes out as dumped, and the service's validator refuses
    the call."""
    value = _Priced.model_construct(price=Decimal(text))

    sent = _encode(value, _Priced) if path == "generated client" else wire_models(value)

    assert sent == {"price": text}
    with pytest.raises(PydanticValidationError) as refused:
        _Priced.model_validate(sent)
    assert [error["type"] for error in refused.value.errors()] == ["finite_number"]
