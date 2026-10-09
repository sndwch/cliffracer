"""A settings key the schema says is unreadable is accepted when pydantic is seen to read it.

`normalize` refuses a field whose written key is not one the model's schema reads. The schema stored
on a model does not always describe what pydantic runs: when one stdlib dataclass is used three or
more times, pydantic shares a single definition and reads each use through it, and a use in a model
that configures aliases differently is read under another config than the schema's nodes show. The
check then refused documents that pydantic writes and reads back. Where it would refuse a field it
now changes a value under a key of the field's object and accepts the field under that key only if
validating the changed document moves that field and no other, and the result dumps the change back
at that key and at no other: positive evidence that the field is carried by the key. A field whose
key is not read, one a validator reads from another key, or one for which no different valid value
can be built, is refused as before.
"""

import dataclasses
import json
from typing import Annotated, Literal

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    computed_field,
    create_model,
    field_serializer,
    model_serializer,
    model_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass

from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import TemplateError
from tests.fixtures.shipment_templates import shipment_template

pytestmark = pytest.mark.unit


def to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


CAMEL = ConfigDict(alias_generator=to_camel, populate_by_name=True)
ALIAS_ONLY = ConfigDict(alias_generator=to_camel)
UNREADABLE = ConfigDict(alias_generator=to_camel, validate_by_alias=False, validate_by_name=True)


@dataclasses.dataclass
class Leaf:
    box_width: int = 1


@dataclasses.dataclass
class Mid:
    box_leaf: Leaf = dataclasses.field(default_factory=Leaf)
    other_leaf: Leaf = dataclasses.field(default_factory=Leaf)


class Plain(BaseModel):
    c_field: Leaf = Leaf()


class Items(BaseModel):
    items: list[Mid] = [Mid()]


def registered(model):
    return TemplateCatalog().register(shipment_template(settings_model=model))


def document_of(instance):
    return json.loads(instance.model_dump_json(by_alias=True, round_trip=True))


def with_leaf_widths(instance, widths):
    """The instance with every `Leaf` it holds set to the next of `widths`, so no field is default."""
    queue = iter(widths)

    def visit(value):
        if isinstance(value, Leaf):
            value.box_width = next(queue)
        elif isinstance(value, BaseModel):
            for name in type(value).model_fields:
                visit(getattr(value, name))
        elif dataclasses.is_dataclass(value):
            for field in dataclasses.fields(value):
                visit(getattr(value, field.name))
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(instance)
    return instance


@pydantic_dataclass(config=ALIAS_ONLY)
class AliasOnlyHolder:
    box_f0_x: Mid = dataclasses.field(default_factory=Mid)


@pydantic_dataclass(config=CAMEL)
class CamelHolder:
    box_f0_x: Mid = dataclasses.field(default_factory=Mid)


def shared_under_two_configs():
    """`Leaf` is read through one shared definition, built under the alias-only holder's config."""
    return create_model("Shared", r0=(Items, Items()), r1=(AliasOnlyHolder, AliasOnlyHolder()))


def shared_under_an_unreadable_root():
    return create_model(
        "SharedUnderUnreadable",
        __config__=UNREADABLE,
        r0=(CamelHolder, CamelHolder()),
        r1=(Leaf, Leaf()),
        r2=(Plain, Plain()),
    )


def shared_under_an_unreadable_root_of(leaf):
    """The shared-definition root, with `leaf` as the dataclass used under all three holders."""

    @dataclasses.dataclass
    class Middle:
        box_leaf: leaf = dataclasses.field(default_factory=leaf)  # type: ignore[valid-type]
        other_leaf: leaf = dataclasses.field(default_factory=leaf)  # type: ignore[valid-type]

    @pydantic_dataclass(config=CAMEL)
    class Holder:
        box_f0_x: Middle = dataclasses.field(default_factory=Middle)

    class Inner(BaseModel):
        c_field: leaf = leaf()  # type: ignore[valid-type]

    return create_model(
        "SharedOf",
        __config__=UNREADABLE,
        r0=(Holder, Holder()),
        r1=(leaf, leaf()),
        r2=(Inner, Inner()),
    )


@dataclasses.dataclass
class Label:
    box_label: str = "a"


@dataclasses.dataclass
class Bounded:
    box_width: Annotated[int, Field(le=5)] = 5


Wide = dataclasses.make_dataclass("Wide", [(f"box_field_{i}", int, 1) for i in range(150)])


@pytest.mark.parametrize(
    "build", [shared_under_two_configs, shared_under_an_unreadable_root], ids=["alias-only", "root"]
)
@pytest.mark.parametrize("widths", [None, [5, 6, 7, 8, 9]], ids=["defaults", "set"])
def test_a_document_pydantic_reads_back_through_a_shared_definition_is_accepted(build, widths):
    model = build()
    instance = model().model_copy(deep=True)
    if widths is not None:
        with_leaf_widths(instance, widths)
    document = document_of(instance)
    assert model.model_validate(document) == instance, "the case is not one pydantic reads back"

    accepted = registered(model).normalize(document)

    assert accepted.materialize() == instance


def test_CONTROL_a_key_pydantic_does_not_read_is_refused_for_a_default_and_for_a_set_value():
    model = create_model("Unreadable", __config__=UNREADABLE, r1=(Leaf, Leaf()))
    for width in (1, 5):
        instance = model().model_copy(deep=True)
        instance.r1.box_width = width

        with pytest.raises(TemplateError, match="box_width.*round-trip"):
            registered(model).normalize(document_of(instance))


def test_CONTROL_a_read_field_with_no_different_valid_value_is_refused():
    @dataclasses.dataclass
    class Constant:
        box_kind: Literal["only"] = "only"
        box_width: int = 1

    @pydantic_dataclass(config=CAMEL)
    class Holder:
        box_f0_x: Constant = dataclasses.field(default_factory=Constant)
        box_f1_x: Constant = dataclasses.field(default_factory=Constant)

    class Inner(BaseModel):
        c_field: Constant = Constant()

    model = create_model(
        "ConstantShared",
        __config__=UNREADABLE,
        r0=(Holder, Holder()),
        r1=(Constant, Constant()),
        r2=(Inner, Inner()),
    )
    document = document_of(model())
    assert model.model_validate(document) == model(), "the case is not one pydantic reads back"

    with pytest.raises(TemplateError, match="box_kind.*round-trip"):
        registered(model).normalize(document)


def test_CONTROL_a_field_that_cannot_be_set_on_construction_is_refused():
    @dataclasses.dataclass
    class Derived:
        box_width: int = dataclasses.field(default=1, init=False)

    model = create_model("Derived", r1=(Derived, Derived()))

    with pytest.raises(TemplateError, match="box_width.*round-trip"):
        registered(model).normalize({"r1": {"box_width": 1}})


def test_CONTROL_a_key_that_is_extra_data_and_not_a_field_is_refused():
    class Loose(BaseModel):
        model_config = ConfigDict(
            alias_generator=to_camel, validate_by_alias=False, validate_by_name=True, extra="allow"
        )
        box_width: int = 1

    with pytest.raises(TemplateError):
        registered(Loose).normalize({"boxWidth": 1})


def test_CONTROL_a_document_the_schema_already_accepts_is_not_probed(monkeypatch):
    from cliffracer.runners import templates

    def never(*args, **kwargs):
        raise AssertionError("the rescue ran for a document the check accepts")

    monkeypatch.setattr(templates._Probe, "key_read_by", never, raising=False)
    model = create_model("Plain", p=(Plain, Plain()))

    registered(model).normalize({"p": {"c_field": {"box_width": 3}}})


def test_CONTROL_a_key_that_changes_another_field_is_not_evidence_for_this_one():
    @dataclasses.dataclass
    class Pair:
        box_width: int = 1
        width: int = 1

    model = create_model("Pair", __config__=UNREADABLE, r1=(Pair, Pair()))

    with pytest.raises(TemplateError, match="box_width.*round-trip"):
        registered(model).normalize({"r1": {"boxWidth": 1, "width": 1}})


def test_CONTROL_a_probe_that_runs_out_of_validations_refuses(monkeypatch):
    from cliffracer.runners import templates

    monkeypatch.setattr(templates._Probe, "BUDGET", 0)
    monkeypatch.setattr(templates._Probe, "PER_SCALAR", 0)

    with pytest.raises(TemplateError, match="box_width.*round-trip"):
        registered(shared_under_an_unreadable_root()).normalize(
            document_of(
                with_leaf_widths(
                    shared_under_an_unreadable_root()().model_copy(deep=True), [5, 6, 7, 8, 9]
                )
            )
        )


@dataclasses.dataclass
class StrictFlag:
    box_flag: Annotated[bool, Field(strict=True)] = True


@dataclasses.dataclass
class Ratio:
    box_ratio: float = 1.5


@dataclasses.dataclass
class AtItsFloor:
    box_width: Annotated[int, Field(ge=0, le=1)] = 0


@dataclasses.dataclass
class AtItsCeiling:
    box_width: Annotated[int, Field(ge=4, le=5)] = 5


@dataclasses.dataclass
class Even:
    box_width: Annotated[int, Field(multiple_of=2)] = 2


@dataclasses.dataclass
class OneCharacterX:
    box_label: Annotated[str, Field(max_length=1)] = "x"


@dataclasses.dataclass
class OneCharacterY:
    box_label: Annotated[str, Field(max_length=1)] = "y"


@dataclasses.dataclass
class Inner:
    width: int = 1


@dataclasses.dataclass
class HoldsADataclass:
    box_inner: Inner = dataclasses.field(default_factory=Inner)


@dataclasses.dataclass
class HoldsAList:
    box_sizes: list[int] = dataclasses.field(default_factory=lambda: [1])


@pytest.mark.parametrize(
    "leaf",
    [
        Label,
        Bounded,
        Wide,
        StrictFlag,
        Ratio,
        AtItsFloor,
        AtItsCeiling,
        Even,
        OneCharacterX,
        OneCharacterY,
        HoldsADataclass,
        HoldsAList,
    ],
    ids=[
        "str-only",
        "constrained",
        "wide",
        "strict-bool",
        "float",
        "int-at-its-floor",
        "int-at-its-ceiling",
        "even-int",
        "one-character-x",
        "one-character-y",
        "nested-dataclass",
        "list",
    ],
)
def test_a_shared_dataclass_of_any_scalar_kind_is_accepted(leaf):
    model = shared_under_an_unreadable_root_of(leaf)
    document = document_of(model())
    assert model.model_validate(document) == model(), "the case is not one pydantic reads back"

    assert registered(model).normalize(document).materialize() == model()


@dataclasses.dataclass
class WithANote:
    box_width: int = 1
    box_note: str | None = None


def test_CONTROL_a_null_under_a_key_is_refused_and_not_walked():
    """A null is not a scalar to change, and not a container to walk into."""
    model = shared_under_an_unreadable_root_of(WithANote)
    document = document_of(model())
    assert model.model_validate(document) == model(), "the case is not one pydantic reads back"

    with pytest.raises(TemplateError, match="box_note.*round-trip"):
        registered(model).normalize(document)


class MovesAHiddenSibling(BaseModel):
    """Changing `xAlias` moves `x` and also `y`, whose serializer writes it as 0 whatever it holds."""

    x: int = Field(default=0, serialization_alias="xAlias")
    y: int = 0

    @field_serializer("y")
    def _always_zero(self, value):
        return 0

    @model_validator(mode="before")
    @classmethod
    def _fill(cls, data):
        if isinstance(data, dict) and "xAlias" in data:
            return {"x": data["xAlias"], "y": data["xAlias"]}
        return data


def test_CONTROL_a_key_that_also_moves_a_sibling_the_dump_hides_is_not_evidence():
    document = {"xAlias": 3, "y": 0}
    assert MovesAHiddenSibling.model_validate(document).model_dump(by_alias=True) == document, (
        "the case is not one pydantic reads back"
    )

    with pytest.raises(TemplateError, match="'x'.*round-trip"):
        registered(MovesAHiddenSibling).normalize(document)


class WrittenUnderItsValidationAlias(BaseModel):
    """`x` is read from `xs`, and a wrap serializer writes it there too, not under `x`."""

    x: int = Field(default=0, validation_alias="xs")

    @model_serializer(mode="wrap")
    def _rename(self, handler):
        written = handler(self)
        return {"xs": written.pop("x"), **written}


def test_a_field_the_probe_finds_under_another_key_is_read_under_that_key():
    accepted = registered(WrittenUnderItsValidationAlias).normalize({"xs": 3})

    assert accepted.materialize() == WrittenUnderItsValidationAlias.model_validate({"xs": 3})


def test_CONTROL_a_probe_with_one_validation_cannot_pass_a_field_that_needs_two(monkeypatch):
    from cliffracer.runners import templates

    model = shared_under_an_unreadable_root_of(Bounded)
    document = document_of(model())
    registered(model).normalize(
        document
    )  # the first value tried, 6, is refused; the second, 4, is read
    monkeypatch.setattr(templates._Probe, "BUDGET", 1)
    monkeypatch.setattr(templates._Probe, "PER_SCALAR", 0)
    monkeypatch.setattr(templates._Probe, "MAX_BUDGET", 1)

    with pytest.raises(TemplateError, match="box_width.*round-trip"):
        registered(model).normalize(document)


def test_CONTROL_the_budget_for_a_wide_document_stops_at_its_cap(monkeypatch):
    from cliffracer.runners import templates

    model = shared_under_an_unreadable_root_of(Wide)
    document = document_of(model())
    monkeypatch.setattr(templates._Probe, "MAX_BUDGET", 50)

    with pytest.raises(TemplateError, match="round-trip"):
        registered(model).normalize(document)


def test_CONTROL_a_key_that_another_field_is_read_from_is_not_this_fields_key():
    class Remapped(BaseModel):
        g: int = 0
        x: int = Field(default=0, serialization_alias="xAlias")

        @model_validator(mode="before")
        @classmethod
        def fill_x_from_g(cls, data):
            if isinstance(data, dict) and "x" not in data and "g" in data:
                return {**data, "x": data["g"]}
            return data

    with pytest.raises(TemplateError, match="x.*round-trip"):
        registered(Remapped).normalize({"g": 5, "x": 5})


class Swapped(BaseModel):
    """Each field is read from the other field's key, so the dump writes each where the other is read."""

    xv: int = Field(default=0, serialization_alias="xs")
    yv: int = Field(default=0, serialization_alias="ys")

    @model_validator(mode="before")
    @classmethod
    def swap(cls, data):
        if isinstance(data, dict) and {"xs", "ys"} & set(data):
            return {"xv": data.get("ys", 0), "yv": data.get("xs", 0)}
        return data


class Rotated(BaseModel):
    a: int = Field(default=0, serialization_alias="aKey")
    b: int = Field(default=0, serialization_alias="bKey")
    c: int = Field(default=0, serialization_alias="cKey")

    @model_validator(mode="before")
    @classmethod
    def rotate(cls, data):
        if isinstance(data, dict) and {"aKey", "bKey", "cKey"} & set(data):
            return {"a": data.get("bKey", 0), "b": data.get("cKey", 0), "c": data.get("aKey", 0)}
        return data


class FromExtra(BaseModel):
    """The field is read from an extra key, and written under its own."""

    model_config = ConfigDict(extra="allow")

    x: int = Field(default=0, serialization_alias="xAlias")

    @model_validator(mode="before")
    @classmethod
    def read_extra(cls, data):
        if isinstance(data, dict) and "e" in data:
            return {**{k: v for k, v in data.items() if k != "xAlias"}, "x": data["e"]}
        return data


class ReadWhereWritten(BaseModel):
    """A before-validator that reads the field from the key the field is written under."""

    x: int = Field(default=0, serialization_alias="xAlias")

    @model_validator(mode="before")
    @classmethod
    def remap(cls, data):
        if isinstance(data, dict) and "xAlias" in data:
            return {"x": data["xAlias"]}
        return data


@pytest.mark.parametrize(
    ("model", "document"),
    [
        (Swapped, {"xs": 3, "ys": 3}),
        (Rotated, {"aKey": 3, "bKey": 3, "cKey": 3}),
        (FromExtra, {"xAlias": 3, "e": 3}),
    ],
    ids=["swap", "rotation", "extra-key"],
)
def test_CONTROL_a_field_read_from_a_key_it_is_not_written_under_is_refused(model, document):
    """Equal values make each document read back as itself, so only the key a field is carried by
    tells these apart: changing the key the field is read from moves the field, but the dump writes
    the change under the field's own key, not the one that was changed."""
    assert model.model_validate(document).model_dump(by_alias=True) == document, (
        "the case is not one pydantic reads back"
    )

    with pytest.raises(TemplateError, match="round-trip"):
        registered(model).normalize(document)


def test_a_field_read_from_the_key_it_is_written_under_is_accepted():
    assert registered(ReadWhereWritten).normalize({"xAlias": 3}).materialize() == (
        ReadWhereWritten(x=3)
    )


Wider = dataclasses.make_dataclass("Wider", [(f"box_field_{i}", int, 1) for i in range(200)])


def test_a_shared_dataclass_that_needs_more_than_the_base_budget_is_accepted(monkeypatch):
    from cliffracer.runners import templates

    model = shared_under_an_unreadable_root_of(Wider)
    document = document_of(model())
    assert registered(model).normalize(document).materialize() == model()

    monkeypatch.setattr(templates._Probe, "PER_SCALAR", 0)
    with pytest.raises(TemplateError, match="round-trip"):
        registered(model).normalize(document)


def test_CONTROL_a_read_that_fails_on_the_changed_document_is_no_evidence():
    """Changing `boxWidth` to 6 makes the union hold `Second`, which has no `box_width`: the read
    fails, and pydantic does not carry `box_width` under that key (the document reloads as 5)."""

    @dataclasses.dataclass
    class First:
        box_width: int = 5

    @pydantic_dataclass(config=CAMEL)
    class Second:
        width: int = Field(alias="boxWidth", ge=6)

    model = create_model("UnionUnderUnreadable", __config__=UNREADABLE, u=(First | Second, First()))
    document = document_of(model())
    assert document == {"u": {"boxWidth": 5}}
    assert isinstance(model.model_validate({"u": {"boxWidth": 6}}).u, Second)

    with pytest.raises(TemplateError, match="box_width.*round-trip"):
        registered(model).normalize(document)


def test_the_probe_stops_copying_when_its_budget_is_spent(monkeypatch):
    """Each copy of the document costs a unit, and each validation one unit plus one per 4,096 bytes
    of the document's JSON, and nothing is copied or validated once the budget is spent: a refusal
    among many keys costs at most the budget, whatever their number."""
    from cliffracer.runners import templates

    class Loose(BaseModel):
        model_config = ConfigDict(
            alias_generator=to_camel, validate_by_alias=False, validate_by_name=True, extra="allow"
        )
        box_width: int = 1

    document = {"boxWidth": 1, **{f"k{i}": [i, i + 1, i + 2] for i in range(400)}}
    copies = []
    real = templates._with_value

    def counting(*args, **kwargs):
        copies.append(1)
        return real(*args, **kwargs)

    validations = []
    real_validated = templates._Probe._validated

    def counting_validations(self, *args, **kwargs):
        validations.append(1)
        return real_validated(self, *args, **kwargs)

    monkeypatch.setattr(templates, "_with_value", counting)
    monkeypatch.setattr(templates._Probe, "_validated", counting_validations)
    monkeypatch.setattr(templates._Probe, "MAX_BUDGET", 100)

    with pytest.raises(TemplateError, match="box_width.*round-trip"):
        registered(Loose).normalize(document)

    # A copy costs one unit, a validation one plus one per 4,096 bytes of this ~9 kB document.
    weight = 1 + len(json.dumps(document)) / 4096
    assert weight > 3, weight
    assert copies and len(copies) + weight * len(validations) <= 100, (copies, validations)


def _fill_xv_from_e(data):
    if isinstance(data, dict) and "e" in data:
        return {**data, "xv": data["e"]}
    return data


class ExtraUnderASerializationAlias(BaseModel):
    """`x` is read from the extra `e` and written as `xs`, which comes back as a stale extra."""

    model_config = ConfigDict(extra="allow")

    x: int = Field(default=0, serialization_alias="xs", validation_alias="xv")

    @model_validator(mode="before")
    @classmethod
    def _fill(cls, data):
        return _fill_xv_from_e(data)


class ExtraBehindAComputedField(BaseModel):
    """`x` is read from the extra `e` and not written at all; a computed field shows it."""

    model_config = ConfigDict(extra="allow")

    x: int = Field(default=0, validation_alias="xv", exclude=True)

    @computed_field(alias="xs")  # type: ignore[prop-decorator]
    @property
    def shown(self) -> int:
        return self.x

    @model_validator(mode="before")
    @classmethod
    def _fill(cls, data):
        return _fill_xv_from_e(data)


class ExtraUnderAWrapSerializer(BaseModel):
    """`x` is read from the extra `e`; the serializer writes it as `xs`, and a stale extra `xs`
    of the same name overrides it in the dict before any JSON is written."""

    model_config = ConfigDict(extra="allow")

    x: int = Field(default=0, validation_alias="xv")

    @model_serializer(mode="wrap")
    def _rename(self, handler):
        written = handler(self)
        return {"xs": written.pop("x"), **written}

    @model_validator(mode="before")
    @classmethod
    def _fill(cls, data):
        return _fill_xv_from_e(data)


@pytest.mark.parametrize(
    "model",
    [ExtraUnderASerializationAlias, ExtraBehindAComputedField, ExtraUnderAWrapSerializer],
    ids=["serialization-alias", "computed-field", "wrap-serializer"],
)
def test_CONTROL_a_field_read_from_an_extra_key_is_refused_whatever_writes_its_own_key(model):
    """Changing `e` moves `x`, and the dump of the changed document shows only `e` changed. Set
    alone on the original instance, `x` changes the dump at its own key, not at `e`."""
    document = json.loads(
        model.model_validate({"e": 5}).model_dump_json(by_alias=True, round_trip=True)
    )

    with pytest.raises(TemplateError, match="'x'.*round-trip"):
        registered(model).normalize(document)


def test_a_validation_costs_by_the_bytes_of_the_document_not_its_scalars(monkeypatch):
    """Forty long strings are forty scalars and two megabytes: each validation reads all of them,
    so it costs by the bytes, and a refusal among them spends its budget in a handful of reads."""
    from cliffracer.runners import templates

    class Loose(BaseModel):
        model_config = ConfigDict(
            alias_generator=to_camel, validate_by_alias=False, validate_by_name=True, extra="allow"
        )
        box_width: int = 1

    document = {"boxWidth": 1, **{f"k{i}": "x" * 50_000 for i in range(40)}}
    validations = []
    real_validated = templates._Probe._validated

    def counting_validations(self, *args, **kwargs):
        validations.append(1)
        return real_validated(self, *args, **kwargs)

    monkeypatch.setattr(templates._Probe, "_validated", counting_validations)

    with pytest.raises(TemplateError, match="box_width.*round-trip"):
        registered(Loose).normalize(document)

    budget = 256 + 8 * 41
    weight = 1 + len(json.dumps(document)) / 4096
    assert weight > 400, weight
    assert len(validations) * weight <= budget, (len(validations), weight)


class MirroredIntoAnExtra(BaseModel):
    """`x` is read from the key it is written under, and a validator also copies that key into an
    extra `shadow`: changing the key changes two written keys."""

    model_config = ConfigDict(extra="allow")

    x: int = Field(default=0, serialization_alias="xAlias")

    @model_validator(mode="before")
    @classmethod
    def _read_and_mirror(cls, data):
        if isinstance(data, dict) and "xAlias" in data:
            return {"x": data["xAlias"], "shadow": data["xAlias"]}
        return data


def test_CONTROL_a_key_whose_change_is_written_back_under_another_key_too_is_refused():
    """Set alone, `x` changes only `xAlias`; the changed document also changes `shadow`, so the
    dump of the changed document is what refuses it."""
    document = {"xAlias": 3, "shadow": 3}
    assert MirroredIntoAnExtra.model_validate(document).model_dump(by_alias=True) == document

    with pytest.raises(TemplateError, match="'x'.*round-trip"):
        registered(MirroredIntoAnExtra).normalize(document)


def shared_of_width(fields: int):
    leaf = dataclasses.make_dataclass(
        f"Width{fields}", [(f"box_field_{i}", int, 1) for i in range(fields)]
    )
    return shared_under_an_unreadable_root_of(leaf)


def test_a_shared_dataclass_of_300_fields_is_refused_where_299_are_accepted():
    """The budget the page states, held at its edge. Every field of the shared dataclass is probed,
    and each costs a copy, a validation and a dump of the field set alone: 299 fields fit in the
    budget their document earns, and 300 do not, so the document is refused as it is without the
    probe. A dump of the field set alone that cost nothing would let 300 fields through."""
    accepted = shared_of_width(299)
    document = document_of(accepted())
    assert registered(accepted).normalize(document).materialize() == accepted()

    refused = shared_of_width(300)
    document = document_of(refused())
    assert refused.model_validate(document) == refused(), "the case is not one pydantic reads back"
    with pytest.raises(TemplateError, match="round-trip"):
        registered(refused).normalize(document)


class Tracked(BaseModel):
    """Read from the key it is written under, like `ReadWhereWritten`, and dumped with whether it
    has moved since it was validated."""

    x: int = Field(default=0, serialization_alias="xAlias")
    _validated_x: int = PrivateAttr(default=0)

    @model_validator(mode="before")
    @classmethod
    def remap(cls, data):
        if isinstance(data, dict) and "xAlias" in data:
            return {"x": data["xAlias"]}
        return data

    @model_validator(mode="after")
    def snapshot(self):
        self._validated_x = self.x
        return self

    @model_serializer(mode="wrap")
    def with_changed(self, handler):
        dumped = handler(self)
        dumped["changed"] = self.x != self._validated_x
        return dumped


def test_a_field_whose_set_alone_dump_moves_another_key_is_refused():
    """Validating the changed document moves `x` and the dump moves only `xAlias`, but the field set
    alone on the original instance dumps a change at `changed` too. The change must show at the
    field's key and at no other in both dumps, so `x` is refused; `ReadWhereWritten`, the same
    model without the second key, is accepted."""
    document = {"xAlias": 3, "changed": False}
    assert Tracked.model_validate(document).model_dump(by_alias=True) == document, (
        "the case is not one pydantic reads back"
    )
    alone = Tracked.model_validate(document)
    object.__setattr__(alone, "x", 4)
    assert alone.model_dump(by_alias=True) == {"xAlias": 4, "changed": True}, (
        "setting the field alone no longer moves a second key, so this case tests nothing"
    )

    with pytest.raises(TemplateError, match="x.*round-trip"):
        registered(Tracked).normalize(document)
