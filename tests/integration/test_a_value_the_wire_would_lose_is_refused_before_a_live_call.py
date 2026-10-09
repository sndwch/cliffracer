"""Live: every path delivers what `serialize_by_alias` writes, and a value no form carries is refused.

Through `call_rpc`, the proxy and a generated stub, from JSON and msgpack callers (the stub writes
JSON). A refused call never reaches the handler.
"""

import math

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

from cliffracer import CliffracerService, RpcProxy, ServiceConfig, rpc
from cliffracer.client import RpcValidationError as ClientRefused
from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcValidationError

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

SERVICE = "shop_lost_rt"


class SerializedByAliasReadByName(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    x: int = Field(0, serialization_alias="X")


class SerializedByAliasRequired(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    x: int = Field(serialization_alias="X")


class ChoiceIsAnotherFieldsName(BaseModel):
    a: int = Field(0, validation_alias=AliasChoices("b", "a"))
    b: int = 0


class PathHeadIsAnotherFieldsName(BaseModel):
    p: dict = {}
    x: int = Field(0, validation_alias=AliasPath("p", "a"))


class CrossedChain(BaseModel):
    """`a` is read from "b", `b` from "c", and `c` by its name, which is also `b`'s alias."""

    a: int = Field(0, alias="b")
    b: int = Field(0, alias="c")
    c: int = 0


class MB(BaseModel):
    a: int = 0
    p: dict = Field({}, validation_alias=AliasChoices("q", "b"))
    b: int = 0


class MSub(MB):
    """MB reads this class's validation-alias form as other values, and no form as the argument."""

    a: int = Field(0, validation_alias=AliasPath("q", "k"))
    p: dict = Field({})
    b: int = Field(0, alias="a")


class SB(BaseModel):
    x: int = 0


class SS(SB):
    """Writes `x` under `S` and reads it under `z`; SB reads it by name."""

    model_config = ConfigDict(serialize_by_alias=True)
    x: int = Field(0, serialization_alias="S", validation_alias="z")


class HoldsX(BaseModel):
    x: int


class HoldsXY(BaseModel):
    x: int
    y: int


class NestedUnderAnotherFieldsName(BaseModel):
    """`a` is read from "b", which is also `b`'s name: every form reads `a` from `b`'s model."""

    a: HoldsX = Field(alias="b")
    b: HoldsXY


class HoldsNan(BaseModel):
    x: float = 0.0


class Hidden(BaseModel):
    """`b` is read under "A", `a`'s serialization alias; by field name the serializer writes `b` as
    `a`, by alias it writes `b` itself."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    a: int = Field(0, serialization_alias="A", validation_alias=AliasChoices("A", "a"))
    b: int = Field(0, validation_alias=AliasChoices("A", "b"))

    @field_serializer("b")
    def _b(self, value: int, info: SerializationInfo) -> int:
        return value if info.by_alias else self.a


class HoldsHidden(BaseModel):
    inner: Hidden


CALLS: list[str] = []


class Shop(CliffracerService):
    @rpc
    async def by_name(self, item: SerializedByAliasReadByName) -> int:
        return item.x

    @rpc
    async def by_name_required(self, item: SerializedByAliasRequired) -> int:
        return item.x

    @rpc
    async def choice(self, item: ChoiceIsAnotherFieldsName) -> str:
        CALLS.append("choice")
        return f"{item.a}/{item.b}"

    @rpc
    async def path(self, item: PathHeadIsAnotherFieldsName) -> str:
        CALLS.append("path")
        return f"{item.p}/{item.x}"

    @rpc
    async def declares_mb(self, item: MB) -> str:
        CALLS.append("declares_mb")
        return f"{item.a}/{item.p}/{item.b}"

    @rpc
    async def declares_sb(self, item: SB) -> int:
        CALLS.append("declares_sb")
        return item.x

    @rpc
    async def nested(self, item: NestedUnderAnotherFieldsName) -> str:
        CALLS.append("nested")
        return f"{item.a.x}/{item.b.x}"

    @rpc
    async def nan(self, item: HoldsNan) -> bool:
        return math.isnan(item.x)

    @rpc
    async def hidden(self, item: Hidden) -> str:
        CALLS.append("hidden")
        return f"{item.a}/{item.b}"

    @rpc
    async def holds_hidden(self, item: HoldsHidden) -> str:
        CALLS.append("holds_hidden")
        return f"{item.inner.a}/{item.inner.b}"

    @rpc
    async def crossed(self, item: CrossedChain) -> str:
        CALLS.append("crossed")
        return f"{item.a}/{item.b}/{item.c}"


class ShopClient(ServiceClient):
    SERVICE = SERVICE

    async def by_name(self, item: SerializedByAliasReadByName) -> int:
        return await self._call(
            "by_name", {"item": self._encode(item, SerializedByAliasReadByName)}, int
        )

    async def by_name_required(self, item: SerializedByAliasRequired) -> int:
        return await self._call(
            "by_name_required", {"item": self._encode(item, SerializedByAliasRequired)}, int
        )

    async def choice(self, item: ChoiceIsAnotherFieldsName) -> str:
        return await self._call(
            "choice", {"item": self._encode(item, ChoiceIsAnotherFieldsName)}, str
        )

    async def path(self, item: PathHeadIsAnotherFieldsName) -> str:
        return await self._call(
            "path", {"item": self._encode(item, PathHeadIsAnotherFieldsName)}, str
        )

    async def crossed(self, item: CrossedChain) -> str:
        return await self._call("crossed", {"item": self._encode(item, CrossedChain)}, str)

    async def nested(self, item: NestedUnderAnotherFieldsName) -> str:
        return await self._call(
            "nested", {"item": self._encode(item, NestedUnderAnotherFieldsName)}, str
        )

    async def nan(self, item: HoldsNan) -> bool:
        return await self._call("nan", {"item": self._encode(item, HoldsNan)}, bool)

    async def hidden(self, item: Hidden) -> str:
        return await self._call("hidden", {"item": self._encode(item, Hidden)}, str)

    async def holds_hidden(self, item: HoldsHidden) -> str:
        return await self._call("holds_hidden", {"item": self._encode(item, HoldsHidden)}, str)

    async def declares_mb(self, item: MB) -> str:
        return await self._call("declares_mb", {"item": self._encode(item, MB)}, str)

    async def declares_sb(self, item: SB) -> int:
        return await self._call("declares_sb", {"item": self._encode(item, SB)}, int)


class Caller(CliffracerService):
    shop = RpcProxy(SERVICE)


async def _each_path(fmt: str, method: str, item: BaseModel) -> dict[str, object]:
    svc = Shop(ServiceConfig(name=SERVICE, health_port=0))
    caller = Caller(ServiceConfig(name="lost_caller", health_port=0, serialization_format=fmt))
    await svc.start()
    await caller.start()
    client = ShopClient(caller.nc, service=SERVICE, verify=False)
    out: dict[str, object] = {}
    try:
        for how in ("call_rpc", "proxy", "stub"):
            try:
                if how == "call_rpc":
                    out[how] = await caller.call_rpc(SERVICE, method, item=item)
                elif how == "proxy":
                    out[how] = await getattr(caller.shop, method)(item=item)
                else:
                    out[how] = await getattr(client, method)(item)
            except (RpcValidationError, ClientRefused) as exc:
                out[how] = "refused before sending" if "before sending" in str(exc) else "refused"
    finally:
        await caller.stop()
        await svc.stop()
    return out


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
@pytest.mark.parametrize(
    ("method", "item"),
    [
        pytest.param("by_name", SerializedByAliasReadByName(x=5), id="with-a-default"),
        pytest.param("by_name_required", SerializedByAliasRequired(x=5), id="required"),
    ],
)
async def test_every_path_delivers_a_model_serialized_by_alias_and_read_by_name(fmt, method, item):
    assert await _each_path(fmt, method, item) == dict.fromkeys(("call_rpc", "proxy", "stub"), 5)


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
@pytest.mark.parametrize(
    ("method", "item"),
    [
        pytest.param("choice", ChoiceIsAnotherFieldsName.model_validate({"a": 3}), id="choice"),
        pytest.param(
            "path",
            PathHeadIsAnotherFieldsName.model_validate({"p": {"a": 5}}).model_copy(
                update={"p": {"k": 1}}
            ),
            id="path-head",
        ),
        pytest.param(
            "crossed",
            CrossedChain.model_validate({"a": 1, "b": 2, "c": 3}, by_name=True, by_alias=False),
            id="crossed-chain",
        ),
        pytest.param(
            "nested",
            NestedUnderAnotherFieldsName.model_validate(
                {"a": {"x": 1}, "b": {"x": 52, "y": 13}}, by_name=True, by_alias=False
            ),
            id="nested-under-another-fields-name",
        ),
        pytest.param("hidden", Hidden(a=5, b=7), id="a-misread-the-by-name-dump-would-hide"),
        pytest.param(
            "holds_hidden",
            HoldsHidden(inner=Hidden(a=5, b=7)),
            id="the-same-nested",
        ),
    ],
)
async def test_a_value_no_form_carries_is_refused_before_it_reaches_the_handler(fmt, method, item):
    CALLS.clear()

    outcomes = await _each_path(fmt, method, item)

    assert outcomes == dict.fromkeys(("call_rpc", "proxy", "stub"), "refused before sending")
    assert CALLS == []


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
async def test_a_subclass_no_form_of_which_its_base_reads_is_refused_without_an_annotation(fmt):
    """A handler declaring MB. Without an annotation the alias dump would be read by MSub with
    `a` at its default, and by MB as other values; no class reads any form as the argument, so
    `call_rpc` and the proxy refuse before sending. The stub knows MB: MB refuses every dump, and
    the service says so."""
    CALLS.clear()
    value = MSub.model_validate({"a": 38, "p": {"k": 4}, "b": 89}, by_name=True, by_alias=False)

    outcomes = await _each_path(fmt, "declares_mb", value)

    assert outcomes == {
        "call_rpc": "refused before sending",
        "proxy": "refused before sending",
        "stub": "refused",
    }
    assert CALLS == []


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
async def test_a_serialize_by_alias_subclass_its_base_reads_by_name_is_not_delivered_as_a_default(
    fmt,
):
    """A handler declaring SB. Without an annotation the alias dump `{"S": 5}` would be read by SB
    and by SS as `x=0`, and the forms SB reads (`{"x": 5}`) and SS reads (`{"z": 5}`) are each
    read as the default by the other, so `call_rpc` and the proxy refuse before sending. The stub
    knows SB and sends `{"x": 5}`."""
    CALLS.clear()
    value = SS.model_validate({"x": 5}, by_name=True, by_alias=False)

    outcomes = await _each_path(fmt, "declares_sb", value)

    assert outcomes == {
        "call_rpc": "refused before sending",
        "proxy": "refused before sending",
        "stub": 5,
    }
    assert CALLS == ["declares_sb"]


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
async def test_a_nan_is_delivered_on_every_path(fmt):
    """A NaN reads back as a NaN, which `==` calls different from the one sent; it is not refused."""
    assert await _each_path(fmt, "nan", HoldsNan(x=float("nan"))) == dict.fromkeys(
        ("call_rpc", "proxy", "stub"), True
    )
