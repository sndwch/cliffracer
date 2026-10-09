"""`call_rpc`, `call_async` and `call_rpc_no_wait` send each model in the form its class reads back.

These calls have no handler annotation, so the argument's own class stands for the service's
model. `to_jsonable_python` writes a model by alias, and a model that is read by field name
(`validate_by_alias=False`, a `serialization_alias` that differs from the `validation_alias`) was
refused by the service. Each model is now written by alias first, as before, then by field name,
and the first its class reads back as the argument is sent. A model that was accepted goes out as
it did, in either serialization format.

The live tests in `tests/integration/test_call_rpc_sends_a_model_in_the_form_the_service_accepts.py`
send these to a service; this file reads the bytes the call hands to the connection, JSON and
msgpack.
"""

import json
import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FieldSerializationInfo,
    field_serializer,
    field_validator,
)

from cliffracer import CliffracerService, RpcProxy, ServiceConfig
from cliffracer.core.validation import deserialize_payload

pytestmark = pytest.mark.unit


class ByNameOnly(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    item_name: str = Field(alias="itemName")


class SplitAliases(BaseModel):
    model_config = ConfigDict(validate_by_name=True)
    item_name: str = Field(validation_alias="item_in", serialization_alias="itemOut")


class AliasOnly(BaseModel):
    item_name: str = Field(alias="itemName")


class Populatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    item_name: str = Field(alias="itemName")


class Bumped(BaseModel):
    """A validator that is not idempotent: no form reads back equal, both are accepted."""

    model_config = ConfigDict(populate_by_name=True)
    n: int = Field(alias="nN")

    @field_validator("n")
    @classmethod
    def _plus_one(cls, value: int) -> int:
        return value + 1


class NeverEqual(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    item_name: str = Field(alias="itemName")

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("no answer")

    __hash__ = None  # type: ignore[assignment]


class Base(BaseModel):
    x: int = Field(alias="xx")


class SubByName(Base):
    """The handler declares `Base`; this is read by field name only."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class BasePopulatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    x: int = Field(alias="xx")


class SubPopulatableByName(BasePopulatable):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class NeverEqualByName(BaseModel):
    """Read by field name only, and an equality that cannot be answered."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    item_name: str = Field(alias="itemName")

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("no answer")

    __hash__ = None  # type: ignore[assignment]


class AppModel(BaseModel):
    """What a project puts at the top of its models: configuration and no fields."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class ByNameOverAppModel(AppModel):
    item_name: str = Field(alias="itemName")


class WithDefaults(BaseModel):
    note: str = "n"


class ByNameOverDefaults(WithDefaults):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    item_name: str = Field(alias="itemName")


class EmptyMixin(BaseModel):
    pass


class ByNameOverMixin(EmptyMixin):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    item_name: str = Field(alias="itemName")


class SwapBase(BaseModel):
    """Aliases that are each other's names: a base that reads the alias form as the argument and
    the field-name form with the two values exchanged."""

    a: str = Field(alias="b")
    b: str = Field(alias="a")


class SwapSubByName(SwapBase):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class Level1(BaseModel):
    x: int = Field(alias="xx")


class Level2(Level1):
    model_config = ConfigDict(populate_by_name=True)


class Level3(Level2):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class Left(BaseModel):
    note: str = "l"


class Right(BaseModel):
    x: int = Field(alias="xx")


class Both(Left, Right):
    """A diamond-free multiple inheritance: the deciding base is the third class in the MRO."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class ByNameWithADefault(BaseModel):
    """The alias form is accepted by this class and read as the default, not as the argument."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    x: int = Field(7, alias="xx")


class ByNameSwapped(BaseModel):
    """Aliases that are each other's names, read by name only: the alias form is read swapped."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    a: str = Field(alias="b")
    b: str = Field(alias="a")


class FloatBase(BaseModel):
    x: float = Field(alias="xx")


class FloatSubByName(FloatBase):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class AliasOnlyP(BaseModel):
    x: int = Field(alias="xx")


class ByNameOnlyQ(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    x: int = Field(alias="xx")


class PThenQ(AliasOnlyP, ByNameOnlyQ):
    pass


class QThenP(ByNameOnlyQ, AliasOnlyP):
    pass


class StrictByName(BaseModel):
    """Strict and read by field name: its datetime, UUID, Decimal and tuple fields are JSON text and
    arrays on the wire, which python mode refuses and JSON mode reads."""

    model_config = ConfigDict(strict=True, validate_by_alias=False, validate_by_name=True)
    when: datetime = Field(alias="When")
    ident: UUID = Field(alias="Ident")
    amount: Decimal = Field(alias="Amount")
    pair: tuple[int, int] = Field(alias="Pair")


class StrictAppModel(BaseModel):
    model_config = ConfigDict(strict=True, validate_by_alias=False, validate_by_name=True)


class StrictByNameOverAppModel(StrictAppModel):
    when: datetime = Field(alias="When")
    ident: UUID = Field(alias="Ident")


class StrictAliasBase(BaseModel):
    model_config = ConfigDict(strict=True)
    when: datetime = Field(alias="When")


class StrictSubByName(StrictAliasBase):
    """The base reads only the alias form, and only as JSON: python mode refuses its datetime text."""

    model_config = ConfigDict(strict=True, validate_by_alias=False, validate_by_name=True)


class StrictByNameBumped(BaseModel):
    """Strict, read by name, and its validator is not idempotent: no form reads back equal, and the
    field-name form is the one it accepts, as JSON."""

    model_config = ConfigDict(strict=True, validate_by_alias=False, validate_by_name=True)
    when: datetime = Field(alias="When")
    n: int = Field(alias="N")

    @field_validator("n")
    @classmethod
    def _plus_one(cls, value: int) -> int:
        return value + 1


STRICT_WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
STRICT_IDENT = UUID("12345678-1234-5678-1234-567812345678")


class Caller(CliffracerService):
    peer = RpcProxy("peer")


def _caller(fmt: str) -> Caller:
    svc = Caller(ServiceConfig(name="caller", health_port=0, serialization_format=fmt))
    svc.nc = AsyncMock()
    reply = AsyncMock()
    reply.data = json.dumps({"result": "ok"}).encode()
    svc.nc.request.return_value = reply
    svc.nc.publish = AsyncMock()
    return svc


def _body(svc: Caller, fmt: str, *, published: bool = False) -> dict:
    call = svc.nc.publish.call_args if published else svc.nc.request.call_args
    body = deserialize_payload(call.args[1], content_type=None, fallback_format=fmt)
    body.pop("correlation_id", None)
    return body


FORMATS = ["json", "msgpack"]


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize(
    ("kwargs", "wire"),
    [
        pytest.param(
            {"item": ByNameOnly(item_name="x")}, {"item": {"item_name": "x"}}, id="validate-by-name"
        ),
        pytest.param(
            {"item": SplitAliases(item_name="x")},
            {"item": {"item_name": "x"}},
            id="split-aliases",
        ),
        pytest.param(
            {"items": [ByNameOnly(item_name="a")]}, {"items": [{"item_name": "a"}]}, id="list"
        ),
        pytest.param(
            {"items": (ByNameOnly(item_name="a"),)}, {"items": [{"item_name": "a"}]}, id="tuple"
        ),
        pytest.param(
            {"items": {"k": ByNameOnly(item_name="a")}},
            {"items": {"k": {"item_name": "a"}}},
            id="dict",
        ),
        pytest.param(
            {"items": {"k": [ByNameOnly(item_name="a")]}},
            {"items": {"k": [{"item_name": "a"}]}},
            id="dict-of-list",
        ),
    ],
)
async def test_a_model_read_by_field_name_is_sent_by_field_name(fmt, kwargs, wire):
    svc = _caller(fmt)

    await svc.call_rpc("peer", "m", **kwargs)
    assert _body(svc, fmt) == wire

    await svc.call_async("peer", "m", **kwargs)
    assert _body(svc, fmt, published=True) == wire

    await svc.call_rpc_no_wait("peer", "m", **kwargs)
    assert _body(svc, fmt, published=True) == wire


@pytest.mark.parametrize("fmt", FORMATS)
async def test_the_proxy_sends_the_same_form(fmt):
    svc = _caller(fmt)

    await svc.peer.m(item=ByNameOnly(item_name="x"))

    assert _body(svc, fmt) == {"item": {"item_name": "x"}}


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize(
    ("kwargs", "wire"),
    [
        pytest.param(
            {"item": AliasOnly(itemName="x")}, {"item": {"itemName": "x"}}, id="alias-only"
        ),
        pytest.param(
            {"item": Populatable(itemName="x")}, {"item": {"itemName": "x"}}, id="populatable"
        ),
        pytest.param({"item": Bumped(nN=4)}, {"item": {"nN": 5}}, id="non-idempotent-validator"),
        pytest.param(
            {"item": NeverEqual(itemName="x")}, {"item": {"itemName": "x"}}, id="equality-raises"
        ),
        pytest.param(
            {"tags": ("b", "a"), "count": 3, "ratio": 0.5, "note": None},
            {"tags": ["b", "a"], "count": 3, "ratio": 0.5, "note": None},
            id="not-models",
        ),
    ],
)
async def test_a_call_that_was_accepted_is_sent_as_before(fmt, kwargs, wire):
    """Alias first is what `to_jsonable_python` always wrote; a model that reads back changed in
    every form, or whose comparison cannot be answered, still gets that form."""
    svc = _caller(fmt)

    await svc.call_rpc("peer", "m", **kwargs)

    assert _body(svc, fmt) == wire


async def test_the_arguments_the_hooks_see_are_the_callers_objects():
    """Only the bytes change. A send hook is handed the arguments as they were passed."""
    seen = []

    class Spy(CliffracerService):
        pass

    svc = Spy(ServiceConfig(name="caller", health_port=0))
    svc.nc = AsyncMock()
    reply = AsyncMock()
    reply.data = json.dumps({"result": "ok"}).encode()
    svc.nc.request.return_value = reply
    original = svc.container.dispatcher._send_context

    def spy(kind, subject, payload, cid):
        seen.append(payload)
        return original(kind, subject, payload, cid)

    svc.container.dispatcher._send_context = spy
    item = ByNameOnly(item_name="x")

    await svc.call_rpc("peer", "m", item=item)

    assert seen[0]["item"] is item


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize(
    ("item", "wire"),
    [
        pytest.param(SubByName(x=1), {"xx": 1}, id="subclass-by-name-of-an-alias-only-base"),
        pytest.param(
            SwapSubByName(a="1", b="2"),
            {"b": "1", "a": "2"},
            id="base-reads-the-alias-form-and-the-name-form-swapped",
        ),
        pytest.param(Level3(x=1), {"xx": 1}, id="three-levels-the-first-base-reads-both"),
        pytest.param(Both(x=1), {"note": "l", "xx": 1}, id="the-deciding-base-is-the-third-class"),
    ],
)
async def test_a_base_that_reads_only_the_alias_form_keeps_it(fmt, item, wire):
    """The handler's annotation is not known here, so it may declare a base class of the
    instance. A base that reads the alias form as the argument and the field-name form not is
    the handler main sent to, so the alias form stays."""
    svc = _caller(fmt)

    await svc.call_rpc("peer", "m", item=item)

    assert _body(svc, fmt) == {"item": wire}


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize(
    ("item", "wire"),
    [
        pytest.param(ByNameOverAppModel(item_name="x"), {"item_name": "x"}, id="app-model"),
        pytest.param(
            ByNameOverDefaults(item_name="x"),
            {"note": "n", "item_name": "x"},
            id="base-with-only-defaulted-fields",
        ),
        pytest.param(ByNameOverMixin(item_name="x"), {"item_name": "x"}, id="empty-mixin"),
        pytest.param(
            SubPopulatableByName(x=1),
            {"x": 1},
            id="base-that-reads-either-form",
        ),
    ],
)
async def test_a_base_that_cannot_tell_the_forms_apart_does_not_decide_the_form(fmt, item, wire):
    """A base with no fields of its own, only defaulted ones, or one that reads both forms accepts
    whatever it is given, so it says nothing about what the handler wants: the class that
    refuses the alias form decides, and the field-name form is sent."""
    svc = _caller(fmt)

    await svc.call_rpc("peer", "m", item=item)

    assert _body(svc, fmt) == {"item": wire}


async def test_a_model_whose_equality_raises_and_is_read_by_field_name_is_sent_by_field_name():
    """An equality that cannot be answered leaves the form "accepted", not refused: the alias
    form is refused by this class, so the field-name form is the one that goes."""
    svc = _caller("json")

    await svc.call_rpc("peer", "m", item=NeverEqualByName(item_name="x"))

    assert _body(svc, "json") == {"item": {"item_name": "x"}}


class Watched(BaseModel):
    """Records every `by_alias` a dump of it is asked for."""

    item_name: str = Field(alias="itemName")
    asked: ClassVar[list[bool]] = []

    @field_serializer("item_name")
    def _watch(self, value: str, info: FieldSerializationInfo) -> str:
        Watched.asked.append(info.by_alias)
        return value


async def test_a_model_the_alias_form_serves_is_dumped_once_by_alias_and_not_by_name():
    """The common call, a model its own class reads by alias, pays for one dump and one
    validation: the field-name form is not made unless the alias form was not read back."""
    Watched.asked.clear()
    svc = _caller("json")

    await svc.call_rpc("peer", "m", item=Watched(itemName="x"))

    assert Watched.asked == [True]
    assert _body(svc, "json") == {"item": {"itemName": "x"}}


@pytest.mark.parametrize(
    ("item", "wire"),
    [
        pytest.param(ByNameWithADefault(x=1), {"x": 1}, id="alias-form-read-as-the-default"),
        pytest.param(
            ByNameSwapped(a="1", b="2"), {"a": "1", "b": "2"}, id="alias-form-read-swapped"
        ),
    ],
)
async def test_a_form_that_is_accepted_but_read_as_another_value_is_not_sent(item, wire):
    """Acceptance is not enough: the alias form of these validates and is read as the default, or
    with the two values exchanged. Only a form read back as the argument decides."""
    svc = _caller("json")

    await svc.call_rpc("peer", "m", item=item)

    sent = _body(svc, "json")["item"]
    assert sent == wire
    assert type(item).model_validate(sent) == item


async def test_a_value_holding_nan_is_read_back_as_itself_so_the_alias_only_base_decides():
    """A NaN read back is compared as the NaN sent. The alias-only base reads the alias form as the
    argument and the subclass reads only the field-name form, so the base decides and the alias form
    goes, as it did before the forms were chosen."""
    svc = _caller("json")

    await svc.call_rpc("peer", "m", item=FloatSubByName(x=float("nan")))

    sent = _body(svc, "json")["item"]
    assert list(sent) == ["xx"]
    assert math.isnan(FloatBase.model_validate(sent).x)


@pytest.mark.parametrize("cls", [PThenQ, QThenP], ids=["alias-only-first", "by-name-only-first"])
async def test_classes_that_disagree_keep_the_alias_form_whatever_their_order(cls):
    """One base reads only the alias form and the other only the field-name form. Any class that
    reads the alias form and not the other keeps it, so the order of the bases does not matter."""
    svc = _caller("json")

    await svc.call_rpc("peer", "m", item=cls.model_construct(x=1))

    assert _body(svc, "json") == {"item": {"xx": 1}}


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize(
    ("item", "keys"),
    [
        pytest.param(
            StrictByName(when=STRICT_WHEN, ident=STRICT_IDENT, amount=Decimal("1.50"), pair=(1, 2)),
            ["when", "ident", "amount", "pair"],
            id="strict-by-name",
        ),
        pytest.param(
            StrictByNameOverAppModel(when=STRICT_WHEN, ident=STRICT_IDENT),
            ["when", "ident"],
            id="strict-app-model-subclass",
        ),
    ],
)
async def test_a_strict_model_read_by_field_name_is_sent_by_field_name(fmt, item, keys):
    """Python mode refuses the JSON text of a strict datetime, UUID or Decimal and the array of a
    tuple; the forms are read as the service reads them, so the field-name form reads back."""
    svc = _caller(fmt)

    await svc.call_rpc("peer", "m", item=item)

    assert list(_body(svc, fmt)["item"]) == keys


@pytest.mark.parametrize(
    ("item", "keys"),
    [
        pytest.param(StrictSubByName(when=STRICT_WHEN), ["When"], id="strict-base-decides"),
        pytest.param(
            StrictByNameBumped(when=STRICT_WHEN, n=1), ["when", "n"], id="accepted-not-equal"
        ),
    ],
)
async def test_every_read_of_a_form_is_the_services_read(item, keys):
    """A strict base decides only if it is asked as the service asks, JSON mode after python mode;
    and the first form accepted, when none reads back equal, is the first one the service would
    accept."""
    svc = _caller("json")

    await svc.call_rpc("peer", "m", item=item)

    assert list(_body(svc, "json")["item"]) == keys
