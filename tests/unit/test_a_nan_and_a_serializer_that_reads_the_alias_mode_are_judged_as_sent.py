"""The refusal before sending reads a NaN as the NaN it is, and a serializer in the mode it wrote.

A float field holding NaN reads back as a NaN, which `==` calls different from the one sent, so the
value was counted as read wrong and the call refused where it had always been delivered. Values are
compared with NaN equal to NaN.

A serializer may write a field differently by alias and by field name. Then neither dump says what
the receiver should hold: the dump in one mode can match a misread of the form written in the other
(`Hidden`: by field name it writes `b` as `a`, by alias it writes `b`, and the alias form is read
with `b` from `a`'s key), and the form sent can carry the serializer's by-name value where the
caller's is another. Only a serializer that writes the same in every dump (`CopiesAlways`) has its
output taken as the value.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    field_serializer,
)

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.validation import _same, deserialize_payload, wire_models

pytestmark = pytest.mark.unit

NAN = float("nan")
WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class HoldsNan(BaseModel):
    x: float = 0.0


class HoldsNanRequired(BaseModel):
    x: float
    y: int = 1


class Hidden(BaseModel):
    """`b` is read under "A", which is `a`'s serialization alias; by field name the serializer writes
    `b` as `a`, by alias it writes `b` itself."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    a: int = Field(0, serialization_alias="A", validation_alias=AliasChoices("A", "a"))
    b: int = Field(0, validation_alias=AliasChoices("A", "b"))

    @field_serializer("b")
    def _b(self, value: int, info: SerializationInfo) -> int:
        return value if info.by_alias else self.a


class HoldsHidden(BaseModel):
    inner: Hidden


class ReadsAsName(BaseModel):
    """`b` is read under `a`'s names too, so a `b` that holds `a`'s value is a `b` the receiver
    would read from `a`'s key: misread, even where the two dumps agree on what `b` holds."""

    model_config = ConfigDict(populate_by_name=True)
    a: int = Field(0, serialization_alias="A", validation_alias=AliasChoices("A", "a"))
    b: int = Field(0, validation_alias=AliasChoices("A", "a"))


def _publisher(fmt: str) -> CliffracerService:
    svc = CliffracerService(ServiceConfig(name="pub", health_port=0, serialization_format=fmt))
    svc.nc = AsyncMock()
    svc.container.nc = svc.nc
    return svc


def _sent(svc: CliffracerService, fmt: str) -> dict:
    data = svc.nc.publish.await_args.args[1]
    return deserialize_payload(data, content_type=None, fallback_format=fmt)["data"]


def _client_form(value: BaseModel) -> object:
    return ServiceClient(verify=False)._encode(value, type(value))


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(HoldsNan(x=NAN), id="nan-with-a-default"),
        pytest.param(HoldsNanRequired(x=NAN), id="nan-required"),
    ],
)
async def test_a_nan_is_sent_on_every_path(value):
    for form in (wire_models(value), _client_form(value)):
        assert type(value).model_validate(form).x != type(value).model_validate(form).x  # NaN
    for how in ("publish_event", "broadcast_message"):
        svc = _publisher("json")
        await getattr(svc, how)("things.happened", item=value)
        assert _sent(svc, "json")["item"]["x"] != _sent(svc, "json")["item"]["x"]  # NaN


@pytest.mark.parametrize(
    ("value", "detail", "loc"),
    [
        pytest.param(Hidden(a=5, b=7), "value_would_be_misread", ["b"], id="hidden"),
        pytest.param(
            HoldsHidden(inner=Hidden(a=5, b=7)),
            "value_would_be_misread",
            ["inner.b"],
            id="hidden-nested",
        ),
        pytest.param(Hidden(a=0, b=7), "value_would_be_lost", ["b"], id="read-as-its-default"),
        pytest.param(
            ReadsAsName.model_validate({"a": 1, "b": 2}, by_alias=False, by_name=True),
            "value_would_be_misread",
            ["b"],
            id="read-as-another-name",
        ),
    ],
)
async def test_a_misread_the_other_modes_dump_would_hide_is_refused_on_every_path(
    value, detail, loc
):
    """Every form delivers `b=5`, `a`'s value; the by-field-name dump also says 5, the alias form
    that is sent says 7. Refused on `call_rpc`'s path, the client and both publishers. A `b` that
    would be read as `a`'s value is refused even when that value is `b`'s own default, and a `b`
    read under `a`'s names is refused even where both dumps write it alike."""
    with pytest.raises(RpcValidationError) as on_the_wire:
        wire_models(value)
    with pytest.raises(RpcValidationError):
        _client_form(value)
    for how in ("publish_event", "broadcast_message"):
        svc = _publisher("json")
        with pytest.raises(RpcValidationError):
            await getattr(svc, how)("things.happened", item=value)
        svc.nc.publish.assert_not_awaited()

    assert [(d["type"], d["loc"]) for d in on_the_wire.value.details] == [(detail, loc)]


def test_two_sequences_are_the_same_only_item_by_item_at_one_length():
    """A list or a tuple is compared item by item with NaN equal to NaN, at one length only, and
    never with a value that is not a sequence; `misread_values` would swallow a raise here and
    drop the field, so a raise is as wrong as a wrong answer."""
    nan, other_nan = float("nan"), float("nan")

    assert _same([nan, 1.0], [other_nan, 1.0])
    assert _same((nan, 1.0), (other_nan, 1.0))
    assert _same([nan], (other_nan,))
    assert _same((nan,), [other_nan])
    assert not _same([1.0], [2.0])
    assert not _same([1.0, 2.0], [1.0, 3.0])
    assert not _same([1.0], [1.0, 2.0])
    assert not _same([5], 5)
    assert not _same(5, [5])


def test_two_dicts_are_the_same_only_with_the_same_keys_and_the_same_values():
    """A dict is compared by its keys and then value by value with NaN equal to NaN, and never with
    a value that is not a dict."""
    nan, other_nan = float("nan"), float("nan")

    assert _same({"k": nan}, {"k": other_nan})
    assert not _same({"k": 1.0}, {"k": 2.0})
    assert not _same({"k": 1}, {"k": 1, "j": 2})
    assert not _same({"k": 1, "j": 2}, {"k": 1})
    assert not _same({"k": 1}, 5)
    assert not _same(5, {"k": 1})


class ByNameWritesAnotherField(BaseModel):
    """By field name the serializer writes `x` as `y`; by alias it writes `x`. The alias form is
    refused (`Y` is an extra key), so the field-name form is the one that would be sent."""

    model_config = ConfigDict(extra="forbid")
    y: int = Field(serialization_alias="Y")
    x: int

    @field_serializer("x")
    def _x(self, value: int, info: SerializationInfo) -> int:
        return value if info.by_alias else self.y


class ByNameWritesAnotherValue(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    x: int = Field(serialization_alias="X")

    @field_serializer("x")
    def _x(self, value: int, info: SerializationInfo) -> int:
        return value if info.by_alias else value + 100


class CopiesAlways(BaseModel):
    a: int = 0
    b: int = 0

    @field_serializer("b")
    def _b(self, value: int) -> int:
        return self.a


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(ByNameWritesAnotherField(x=3, y=5), id="by-name-writes-another-field"),
        pytest.param(ByNameWritesAnotherValue(x=5), id="by-name-writes-another-value"),
    ],
)
async def test_a_serializer_that_writes_by_field_name_only_does_not_vouch_for_the_form(value):
    """The form that would be sent carries the serializer's by-name output (`x=5` for a caller's 3,
    or 105 for 5); the dump by alias says otherwise, so nothing says the receiver should hold it."""
    with pytest.raises(RpcValidationError):
        wire_models(value)
    with pytest.raises(RpcValidationError):
        _client_form(value)
    for how in ("publish_event", "broadcast_message"):
        svc = _publisher("json")
        with pytest.raises(RpcValidationError):
            await getattr(svc, how)("things.happened", item=value)


async def test_a_serializer_that_writes_the_same_in_every_dump_is_sent_its_output():
    """`b` is written as `a` by alias and by field name alike: the receiver gets the serializer's
    output, as it did."""
    value = CopiesAlways(a=5, b=7)

    assert wire_models(value) == {"a": 5, "b": 5}
    assert _client_form(value) == {"a": 5, "b": 5}
    svc = _publisher("json")
    await svc.publish_event("things.happened", item=value)
    assert _sent(svc, "json")["item"] == {"a": 5, "b": 5}


class AliasedLevel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    x: int = Field(0, alias="X")


class CopiesAlwaysBesideANestedModel(CopiesAlways):
    inner: AliasedLevel


class CopiesAlwaysUnderItsAlias(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    a: int = 0
    b: int = Field(0, alias="B")

    @field_serializer("b")
    def _b(self, value: int) -> int:
        return self.a


class Level(BaseModel):
    n: int = 0


class CopiesAlwaysBesideTwinLevels(CopiesAlways):
    """Two nested models that hold equal values: each is read right, not as the other's value."""

    x: Level = Level()
    y: Level = Level()


def _b_of(form: dict) -> object:
    """`b` as a form carries it: under its alias "B" where that form is written by alias."""
    return form["B"] if "B" in form else form["b"]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(
            CopiesAlwaysBesideANestedModel(a=5, b=7, inner=AliasedLevel(x=1)),
            id="beside-a-required-nested-model",
        ),
        pytest.param(CopiesAlwaysUnderItsAlias(a=5, b=7), id="under-its-own-alias"),
        pytest.param(
            CopiesAlwaysBesideTwinLevels(a=5, b=7, x=Level(n=1), y=Level(n=1)),
            id="beside-two-equal-nested-models",
        ),
    ],
)
async def test_a_serializer_that_writes_the_same_in_every_dump_is_sent_its_output_beside_others(
    value,
):
    assert _b_of(wire_models(value)) == 5
    assert _b_of(_client_form(value)) == 5
    svc = _publisher("json")
    await svc.publish_event("things.happened", item=value)
    assert _b_of(_sent(svc, "json")["item"]) == 5


def test_two_equal_nested_models_are_each_read_right():
    value = CopiesAlwaysBesideTwinLevels(a=5, b=7, x=Level(n=1), y=Level(n=1))

    assert wire_models(value) == {"a": 5, "b": 5, "x": {"n": 1}, "y": {"n": 1}}


class HiddenOptionalLevels(BaseModel):
    """`Hidden`, with optional nested models for values: by field name the serializer writes `b` as
    `a`, by alias `b` itself."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    a: Level | None = Field(None, serialization_alias="A", validation_alias=AliasChoices("A", "a"))
    b: Level | None = Field(None, validation_alias=AliasChoices("A", "b"))

    @field_serializer("b")
    def _b(self, value: Level | None, info: SerializationInfo) -> Level | None:
        return value if info.by_alias else self.a


async def test_a_misread_between_a_model_and_none_is_refused_on_every_path():
    """`b` holds a model and would be read as `a`'s None: only one side of the comparison is a
    model, so it is compared as a value, not as a model read right."""
    value = HiddenOptionalLevels(a=None, b=Level(n=1))

    with pytest.raises(RpcValidationError):
        wire_models(value)
    with pytest.raises(RpcValidationError):
        _client_form(value)
    for how in ("publish_event", "broadcast_message"):
        svc = _publisher("json")
        with pytest.raises(RpcValidationError):
            await getattr(svc, how)("things.happened", item=value)
        svc.nc.publish.assert_not_awaited()


class WritesZUnderY(BaseModel):
    """`z` is written under "y" by alias, and by field name its serializer writes it as `y`."""

    z: str = Field(alias="y")

    @field_serializer("z")
    def _z(self, value: str, info: SerializationInfo) -> str:
        return value if info.by_alias else self.y


class SharesAKeyAndCopiesByName(WritesZUnderY):
    """Adds `y`, read at `p.k` and by field name. By alias `z` and `y` are both written under "y",
    so that dump holds `y`'s value there; by field name `z` is written as `y`. The two dumps agree
    on `z` by coincidence, and neither holds the caller's `z`."""

    model_config = ConfigDict(validate_by_name=True, validate_by_alias=False)
    y: str = Field("d", validation_alias=AliasPath("p", "k"))


def test_dumps_that_agree_on_a_field_two_fields_write_under_one_key_vouch_for_nothing():
    value = SharesAKeyAndCopiesByName.model_validate(
        {"z": "v1", "y": "V3"}, by_name=True, by_alias=False
    )

    with pytest.raises(RpcValidationError):
        _client_form(value)


class StrictNan(BaseModel):
    """Strict, with a datetime: the receiver reads it in JSON mode, which builds a new NaN."""

    model_config = ConfigDict(strict=True)
    when: datetime
    x: float


class HoldsStrictNan(BaseModel):
    inner: StrictNan


class ListsStrictNan(BaseModel):
    """A list of models is compared item by item, each by its field values."""

    model_config = ConfigDict(strict=True)
    when: datetime
    items: list[StrictNan]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(StrictNan(when=WHEN, x=NAN), id="read-as-json"),
        pytest.param(HoldsStrictNan(inner=StrictNan(when=WHEN, x=NAN)), id="nested-read-as-json"),
        pytest.param(
            ListsStrictNan(when=WHEN, items=[StrictNan(when=WHEN, x=NAN)]), id="list-read-as-json"
        ),
    ],
)
async def test_a_nan_read_back_as_a_new_nan_is_still_the_value_sent(value):
    """JSON mode builds a new float for the NaN, not the object that was sent; it is the same value."""
    for form in (wire_models(value), _client_form(value)):
        assert form is not None
    svc = _publisher("json")
    await svc.publish_event("things.happened", item=value)
    svc.nc.publish.assert_awaited()


class ByAliasWritesAnotherField(BaseModel):
    """By alias `a` is written as `b`; `b` is written as `a` in every dump. The dump by alias alone
    says `a` is 7; the dump by field name says 1, the caller's."""

    a: int = 0
    b: int = 0

    @field_serializer("a")
    def _a(self, value: int, info: SerializationInfo) -> int:
        return self.b if info.by_alias else value

    @field_serializer("b")
    def _b(self, value: int) -> int:
        return self.a


class ByAliasWritesAnotherValue(BaseModel):
    """Read only by alias, and written by alias as `a + 100`: the only form the receiver accepts
    carries 101 for the caller's 1."""

    model_config = ConfigDict(serialize_by_alias=True)
    a: int = Field(0, alias="A")

    @field_serializer("a")
    def _a(self, value: int, info: SerializationInfo) -> int:
        return value + 100 if info.by_alias else value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(ByAliasWritesAnotherField(a=1, b=7), id="by-alias-writes-another-field"),
        pytest.param(ByAliasWritesAnotherValue(A=1), id="by-alias-writes-another-value"),
    ],
)
def test_a_serializer_that_writes_by_alias_only_does_not_vouch_for_the_form(value):
    """The dump by alias alone would vouch for what the alias form carries (`a=7`, `a=101`); the
    dump by field name holds the caller's value, so nothing vouches, and the call is refused."""
    with pytest.raises(RpcValidationError):
        wire_models(value)
