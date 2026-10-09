"""`normalize` copies settings that hold a set of dataclasses, and refuses a lossy serializer by name.

A settings model is copied in Python mode, so a retry keeps the validated Python types. A field
holding a set or frozenset of dataclasses cannot be dumped so (each item becomes a dict, which does
not hash): its value is copied as it is, and every other field and extra is dumped as before. A
model whose serializer writes a value it cannot read back, or a different value it reads back as
that (`abs` of -1), is refused with `TemplateError`, as every lossy serializer is; a mapping the
caller passes that the model refuses is still the caller's `ValidationError`.
"""

import dataclasses
import datetime
import decimal
import enum
import pathlib
import uuid
from typing import Annotated, Any

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    RootModel,
    ValidationError,
    create_model,
    field_serializer,
)

from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import TemplateError
from tests.fixtures.shipment_templates import shipment_template

pytestmark = pytest.mark.unit


def _registered(model):
    return TemplateCatalog().register(shipment_template(settings_model=model))


@dataclasses.dataclass(frozen=True)
class Box:
    box_width: int = 1


@dataclasses.dataclass
class Shelf:
    boxes: set[Box] = dataclasses.field(default_factory=lambda: {Box(2)})


class InASet(BaseModel):
    boxes: set[Box] = Field(default_factory=lambda: {Box(), Box(3)})
    count: int = 0


class InAFrozenset(BaseModel):
    boxes: frozenset[Box] = Field(default_factory=lambda: frozenset({Box(4)}))


class InADataclass(BaseModel):
    shelf: Shelf = Field(default_factory=Shelf)


class UnderAnAlias(BaseModel):
    """Copied under the key its dump writes, which is the key it reads."""

    boxes: set[Box] = Field(default_factory=lambda: {Box(6)}, alias="theBoxes")


class ASetAtTheRoot(RootModel[set[Box]]):
    root: set[Box] = Field(default_factory=lambda: {Box(7)})


@pytest.mark.parametrize(
    "model",
    [InASet, InAFrozenset, InADataclass, UnderAnAlias, ASetAtTheRoot],
    ids=lambda m: m.__name__,
)
def test_settings_holding_a_set_of_dataclasses_are_accepted_and_read_back(model):
    settings = model()

    normalized = _registered(model).normalize(settings)

    assert normalized.materialize() == settings


def test_a_set_under_an_alias_is_copied_under_the_key_it_is_read_from():
    """A value other than the default, so that one dropped from the copy cannot pass for it."""
    settings = UnderAnAlias(theBoxes={Box(8)})

    assert _registered(UnderAnAlias).normalize(settings).materialize() == settings


def test_a_retry_with_defaults_keeps_the_set_of_dataclasses():
    registered = _registered(InASet)
    accepted = registered.normalize(InASet(boxes={Box(5)}, count=1))

    retried = registered.normalize({"count": 2}, defaults=accepted).materialize()

    assert retried.boxes == {Box(5)} and isinstance(retried.boxes, set)
    assert retried.count == 2


def _outcome(model, settings) -> str:
    try:
        return "accepted " + _registered(model).normalize(settings)._json
    except TemplateError as refused:
        return f"refused: {refused}"


class KeepsExtras(BaseModel):
    model_config = ConfigDict(extra="allow")

    boxes: frozenset[Box] = Field(default_factory=lambda: frozenset({Box(1)}))


def test_an_extra_beside_a_set_of_dataclasses_is_kept():
    settings = KeepsExtras(note="keep-me")

    assert _registered(KeepsExtras).normalize(settings).materialize() == settings


def test_an_extra_holding_a_set_of_dataclasses_is_refused_by_name_not_by_a_crash():
    """An extra has no type to read it back as a set, so it is refused, after being copied."""
    with pytest.raises(TemplateError, match="round-trip"):
        _registered(KeepsExtras).normalize(KeepsExtras(more=frozenset({Box(2)})))


def _twins(name: str, **fields: Any) -> tuple[type[BaseModel], type[BaseModel]]:
    """One model with a frozenset of dataclasses and one with a list of them, `fields` beside each."""
    holding = {
        "set": (frozenset[Box], Field(default_factory=lambda: frozenset({Box(1)}))),
        "list": (list[Box], Field(default_factory=lambda: [Box(1)])),
    }
    return (
        create_model(f"{name}Set", boxes=holding["set"], **fields),
        create_model(f"{name}List", boxes=holding["list"], **fields),
    )


@pytest.mark.parametrize(
    ("name", "fields", "given"),
    [
        ("Excluded", {"hidden": (int, Field(0, exclude=True))}, {"hidden": 5}),
        ("Sorted", {"tags": (Annotated[list[str], PlainSerializer(sorted)], ["b", "a"])}, {}),
        (
            "AsText",
            {"price": (Annotated[decimal.Decimal, PlainSerializer(str)], decimal.Decimal("1.5"))},
            {},
        ),
    ],
    ids=["an-excluded-field", "a-sorting-serializer", "a-serializer-to-text"],
)
def test_the_fields_beside_a_set_are_copied_as_they_are_beside_a_list(name, fields, given):
    """Every field but the set is dumped as it is when nothing fails, so a model with a set and the
    same model with a list come out the same, but for the container."""
    with_a_set, with_a_list = _twins(name, **fields)

    as_set = _outcome(with_a_set, with_a_set(**given)).replace(with_a_set.__name__, "M")
    as_list = _outcome(with_a_list, with_a_list(**given)).replace(with_a_list.__name__, "M")

    assert as_set == as_list, (as_set, as_list)


class SerializesTheSet(BaseModel):
    boxes: frozenset[Box] = Field(default_factory=lambda: frozenset({Box(1)}))

    @field_serializer("boxes")
    def _as_a_set(self, value: frozenset[Box]) -> set[Box]:
        return set(value)


def test_a_set_field_whose_own_serializer_fails_is_refused_not_copied_past_it():
    with pytest.raises(TemplateError, match="round-trip"):
        _registered(SerializesTheSet).normalize(SerializesTheSet())


class Absolute(BaseModel):
    count: Annotated[int, PlainSerializer(abs)] = -1


def test_a_model_whose_serializer_changes_a_value_is_refused_though_the_change_reads_back():
    """`abs` writes 1 for -1, and 1 reads back as 1: the copy the model's own dump made already holds
    1, so what is stored is compared with the caller's model too."""
    with pytest.raises(TemplateError, match="round-trip"):
        _registered(Absolute).normalize(Absolute())


class AbsoluteWithMore(Absolute):
    """A subclass the template's settings model reads back as: its own field is not stored."""

    more: int = 0


def test_a_subclass_instance_whose_serializer_changes_a_value_is_refused():
    with pytest.raises(TemplateError, match="round-trip"):
        _registered(Absolute).normalize(AbsoluteWithMore())


class TextWithMore(BaseModel):
    price: Annotated[decimal.Decimal, PlainSerializer(str)] = decimal.Decimal("1.5")


class TextWithMoreSubclass(TextWithMore):
    more: int = 0


def test_CONTROL_a_subclass_instance_whose_serializer_changes_only_the_form_is_accepted():
    accepted = _registered(TextWithMore).normalize(TextWithMoreSubclass()).materialize()

    assert accepted == TextWithMore()


def test_CONTROL_the_same_value_as_a_mapping_is_refused_as_before():
    with pytest.raises(TemplateError, match="round-trip"):
        _registered(Absolute).normalize({"count": -1})


class WrittenAsText(BaseModel):
    model_config = ConfigDict(extra="allow")

    count: Annotated[int, PlainSerializer(abs)] = 1
    when: Annotated[datetime.datetime, PlainSerializer(lambda v: v.isoformat())] = (
        datetime.datetime(2020, 1, 2)
    )
    price: Annotated[decimal.Decimal, PlainSerializer(str)] = decimal.Decimal("1.5")


def test_CONTROL_a_serializer_that_changes_the_form_but_not_the_value_is_accepted():
    settings = WrittenAsText(note="kept")

    assert _registered(WrittenAsText).normalize(settings).materialize() == settings


class Colour(enum.Enum):
    RED = "red"


STRICT_VALUES: dict[str, tuple[Any, Any]] = {
    "datetime": (datetime.datetime, datetime.datetime(2020, 1, 2, 3, 4, 5)),
    "date": (datetime.date, datetime.date(2020, 1, 2)),
    "timedelta": (datetime.timedelta, datetime.timedelta(seconds=5)),
    "uuid": (uuid.UUID, uuid.UUID(int=5)),
    "decimal": (decimal.Decimal, decimal.Decimal("1.5")),
    "bytes": (bytes, b"ab"),
    "tuple": (tuple[int, int], (1, 2)),
    "set": (set[int], {1, 2}),
    "frozenset": (frozenset[int], frozenset({3})),
    "enum": (Colour, Colour.RED),
    "path": (pathlib.Path, pathlib.Path("/a/b")),
}


@pytest.mark.parametrize("name", list(STRICT_VALUES))
def test_CONTROL_a_strict_models_python_values_are_copied_as_python_values(name):
    """A copy through JSON would hand a strict model strings and lists it refuses."""
    kind, value = STRICT_VALUES[name]
    model = create_model(f"Strict_{name}", __config__=ConfigDict(strict=True), x=(kind, value))
    registered = _registered(model)

    accepted = registered.normalize(model())
    retried = registered.normalize({}, defaults=accepted).materialize()

    assert accepted.materialize().x == value
    assert retried.x == value and type(retried.x) is type(value)


@dataclasses.dataclass
class Leaf:
    box_width: int = 1


class WritesNothing(BaseModel):
    leaf: Leaf = Field(default_factory=Leaf)

    @field_serializer("leaf")
    def _nothing(self, value: Leaf) -> None:
        return None


class HoldsALossyModel(BaseModel):
    inner: WritesNothing = Field(default_factory=WritesNothing)


@pytest.mark.parametrize(
    "settings",
    [HoldsALossyModel(), {"inner": {"leaf": {"box_width": 1}}}],
    ids=["an-instance", "a-mapping"],
)
def test_a_serializer_the_model_cannot_read_back_is_refused_with_template_error(settings):
    with pytest.raises(TemplateError, match="round-trip"):
        _registered(HoldsALossyModel).normalize(settings)


def test_CONTROL_a_mapping_the_model_refuses_is_the_callers_validation_error():
    with pytest.raises(ValidationError):
        _registered(HoldsALossyModel).normalize({"inner": {"leaf": {"box_width": "wide"}}})
