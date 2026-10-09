"""`publish_event` and `broadcast_message` write each model in the form its class reads back.

They wrote a model by alias, so a listener whose model reads by field name only refused the event. The
publish side now goes through `wire_models`, as the RPC call paths do: the alias form first, as
before, the field-name form when the model does not read the alias form back as itself, and a form
written a level at a time for a tree whose levels need different ones. This file reads the bytes
handed to the connection; the live tests in
`tests/integration/test_an_event_carries_a_model_in_the_form_its_listener_reads.py` deliver them.
"""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.validation import deserialize_payload

pytestmark = pytest.mark.unit


class ByName(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    x: int = Field(alias="xx")


class AliasOnly(BaseModel):
    x: int = Field(alias="xx")


class Populatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    x: int = Field(alias="xx")


class Inner(BaseModel):
    y: int = Field(alias="yy")


class ByNameOverAliasInner(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    x: int = Field(alias="xx")
    inner: Inner


def _publisher(fmt: str) -> CliffracerService:
    svc = CliffracerService(ServiceConfig(name="pub", health_port=0, serialization_format=fmt))
    svc.nc = AsyncMock()
    svc.container.nc = svc.nc
    return svc


def _sent(svc: CliffracerService, fmt: str) -> dict:
    data = svc.nc.publish.await_args.args[1]
    return deserialize_payload(data, content_type=None, fallback_format=fmt)["data"]


FORMATS = ["json", "msgpack"]
SHAPES = [
    pytest.param(ByName(x=1), {"x": 1}, id="by-name-only"),
    pytest.param(AliasOnly(xx=1), {"xx": 1}, id="alias-only"),
    pytest.param(Populatable(xx=1), {"xx": 1}, id="populatable-keeps-the-alias"),
    pytest.param(
        ByNameOverAliasInner(x=1, inner=Inner(yy=2)),
        {"x": 1, "inner": {"yy": 2}},
        id="levels-that-need-different-forms",
    ),
    pytest.param([ByName(x=1)], [{"x": 1}], id="a-list-of-by-name-models"),
]
#: An alias-only model is written by alias whatever `broadcast_message` does with it, so it is not
#: a case there; `publish_event` keeps it.
BROADCAST_SHAPES = [shape for shape in SHAPES if shape.id != "alias-only"]


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize(("value", "wire"), SHAPES)
async def test_publish_event_writes_each_model_in_the_form_its_class_reads(fmt, value, wire):
    svc = _publisher(fmt)

    await svc.publish_event("things.happened", item=value)

    assert _sent(svc, fmt) == {"item": wire}


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize(("value", "wire"), BROADCAST_SHAPES)
async def test_broadcast_message_writes_each_model_in_the_form_its_class_reads(fmt, value, wire):
    svc = _publisher(fmt)

    await svc.broadcast_message("things.happened", item=value)

    assert _sent(svc, fmt) == {"item": wire}


async def test_a_payload_given_as_data_is_written_the_same_way():
    svc = _publisher("json")

    await svc.publish_event("things.happened", data={"item": ByName(x=1)})

    assert _sent(svc, "json") == {"item": {"x": 1}}


async def test_the_hooks_and_the_idempotency_key_see_the_callers_objects():
    """Only the bytes change: a send hook gets the payload as passed, and the idempotency key is
    computed from it, so a key derived before this is derived the same way. An alias-only model is
    written by alias (`{"xx": 1}`) while the key hashes the caller's model (`{"x": 1}`), so a key
    computed from what is written would differ."""
    from cliffracer.core.idempotency import compute_payload_hash

    seen = []
    svc = _publisher("json")
    original = svc.container.dispatcher._send_context

    def spy(kind, subject, payload, cid):
        seen.append(payload)
        return original(kind, subject, payload, cid)

    svc.container.dispatcher._send_context = spy
    item = AliasOnly(xx=1)

    await svc.publish_event("things.happened", idempotent=True, item=item)

    assert seen[0]["data"]["item"] is item
    assert _sent(svc, "json")["item"] == {"xx": 1}
    headers = svc.nc.publish.await_args.kwargs.get("headers") or {}
    assert headers.get("Nats-Msg-Id") == "things.happened:" + compute_payload_hash({"item": item})
    assert compute_payload_hash({"item": item}) != compute_payload_hash({"item": {"xx": 1}})


async def test_values_that_are_not_models_are_written_as_before():
    svc = _publisher("json")

    await svc.publish_event("things.happened", tags=("b", "a"), n=3, ratio=0.5, note=None)

    assert _sent(svc, "json") == {"tags": ["b", "a"], "n": 3, "ratio": 0.5, "note": None}
    assert json.loads(svc.nc.publish.await_args.args[1])["data"]["tags"] == ["b", "a"]


class CrossedChain(BaseModel):
    """`a` is read from "b", `b` from "c", and `c` by its name, which is also `b`'s alias."""

    a: int = Field(0, alias="b")
    b: int = Field(0, alias="c")
    c: int = 0


@pytest.mark.parametrize("how", ["publish_event", "broadcast_message"])
async def test_an_event_no_form_of_which_carries_its_values_is_refused_before_publishing(how):
    """Every form would deliver `b` holding `c`'s value: the publisher refuses, naming `b`, and
    nothing is published."""
    from cliffracer.core.exceptions import RpcValidationError

    svc = _publisher("json")
    value = CrossedChain.model_validate({"a": 1, "b": 2, "c": 3}, by_name=True, by_alias=False)

    with pytest.raises(RpcValidationError) as refused:
        await getattr(svc, how)("things.happened", item=value)

    assert [(d["type"], d["loc"]) for d in refused.value.details] == [
        ("value_would_be_misread", ["b"])
    ]
    svc.nc.publish.assert_not_awaited()


class InnerCrossed(BaseModel):
    """`f1` is read under "f0", which is also `f0`'s name."""

    model_config = ConfigDict(serialize_by_alias=True)
    f0: int
    f1: int = Field(alias="f0")


class HoldsInnerCrossed(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    f0: InnerCrossed = Field(validation_alias=AliasChoices("f0_v", "f0"))


class TwoInts(BaseModel):
    model_config = ConfigDict(validate_by_alias=True, validate_by_name=False)
    f0: int
    f1: int


class PairIn(BaseModel):
    f0: int
    f1: int


class ThreeIn(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    f0: int = Field(serialization_alias="f0_s")
    f1: int
    f2: int = Field(alias="f2_a")


class Sibling(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    f0: PairIn
    f1: int
    f2: ThreeIn


class ReshapedByAnInnerAlias(BaseModel):
    """`f0` is written under "f0_s" and `f2` under "f0"; inside `f0`, `f1` is read from "f0". The
    receiver reads `f0.f0` as `{"f0": 8, "f1": 8}` in place of `{"f0": 16, "f1": 32}`: a value equal to
    no other field's, which is still not the caller's."""

    model_config = ConfigDict(populate_by_name=True)
    f0: HoldsInnerCrossed = Field(serialization_alias="f0_s")
    f1: TwoInts = Field(validation_alias=AliasChoices("f1_v", "f1"))
    f2: Sibling = Field(alias="f0")


RESHAPED = {
    "f0": {"f0": {"f0": 16, "f1": 32}},
    "f1": {"f0": 90, "f1": 78},
    "f2": {"f0": {"f0": 8, "f1": 79}, "f1": 26, "f2": {"f0": 23, "f1": 92, "f2": 95}},
}


async def test_a_value_an_inner_alias_reshapes_is_refused_on_every_path():
    """Main dead-lettered this event; written in any form, it is read with `f0.f0.f0` and `f0.f0.f1`
    holding values the caller did not set. `call_rpc`'s path and both publishers refuse, naming them."""
    from cliffracer.core.exceptions import RpcValidationError
    from cliffracer.core.validation import wire_models

    value = ReshapedByAnInnerAlias.model_validate(RESHAPED, by_alias=False, by_name=True)

    with pytest.raises(RpcValidationError) as on_the_wire:
        wire_models({"item": value})
    svc = _publisher("json")
    with pytest.raises(RpcValidationError):
        await svc.publish_event("things.happened", item=value)
    with pytest.raises(RpcValidationError):
        await svc.broadcast_message("things.happened", item=value)

    assert [(d["type"], d["loc"]) for d in on_the_wire.value.details] == [
        ("value_would_be_misread", ["f0.f0.f0"]),
        ("value_would_be_misread", ["f0.f0.f1"]),
    ]
    svc.nc.publish.assert_not_awaited()


VALIDATIONS = [0]


class CountedAliasRead(BaseModel):
    x: int = Field(alias="xx")

    @field_validator("x")
    @classmethod
    def _count(cls, value: int) -> int:
        VALIDATIONS[0] += 1
        return value


class CountedByNameRead(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    x: int = Field(alias="xx")

    @field_validator("x")
    @classmethod
    def _count(cls, value: int) -> int:
        VALIDATIONS[0] += 1
        return value


@pytest.mark.parametrize(
    ("value", "wire", "validations"),
    [
        pytest.param(CountedAliasRead(xx=1), {"xx": 1}, 1, id="read-by-alias-one-validation"),
        pytest.param(CountedByNameRead(x=1), {"x": 1}, 3, id="read-by-name-as-before"),
    ],
)
def test_a_model_its_class_reads_by_alias_is_validated_once(value, wire, validations):
    """A model whose class reads its alias form back is written after one validation; one that needs
    another form takes the full choice, its validations unchanged."""
    from cliffracer.core.validation import wire_models

    VALIDATIONS[0] = 0

    assert wire_models(value) == wire
    assert VALIDATIONS[0] == validations


@pytest.mark.parametrize(
    ("value", "checks"),
    [
        pytest.param(CountedAliasRead(xx=1), 1, id="read-by-alias"),
        pytest.param(CountedByNameRead(x=1), 5, id="read-by-name"),
    ],
)
def test_the_alias_form_is_checked_against_the_class_once(value, checks, monkeypatch):
    """The check that decides the common case is not made a second time when it fails: a model its
    class reads by alias is decided by one read-back, and one that needs another form takes five,
    one fewer than when the choice repeated that check."""
    from cliffracer.core import validation

    made = []
    check = validation._reads_as_the_argument

    def counted(cls, wire, held):
        made.append(cls)
        return check(cls, wire, held)

    monkeypatch.setattr(validation, "_reads_as_the_argument", counted)

    validation.wire_models(value)

    assert len(made) == checks
