"""A model read through `AliasChoices` or `AliasPath` reaches a live handler with the values passed.

Every whole-value dump was read as the field's default, so `x=5` arrived as `x=0` and a required
field was refused. Each shape goes through `call_rpc`, the proxy and `call_async`, from a JSON and a
msgpack caller, and through a generated stub (`ServiceClient._encode`); the handler reports what it
read.
"""

import asyncio
import json

import pytest
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, TypeAdapter

from cliffracer import CliffracerService, RpcProxy, ServiceConfig, async_rpc, rpc
from cliffracer.client import RpcValidationError as ClientRefused
from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcValidationError

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

SERVICE = "shop_valias_rt"


class Choices(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices("a", "b"))


class ChoicesPathFirst(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices(AliasPath("outer", 0), "b"))


class Path(BaseModel):
    x: int = Field(0, validation_alias=AliasPath("outer", 0))


class DeepPath(BaseModel):
    x: int = Field(0, validation_alias=AliasPath("a", "b", 1))


class RequiredPath(BaseModel):
    x: int = Field(validation_alias=AliasPath("outer", "x"))


class Mixed(BaseModel):
    y: int = Field(alias="yy")
    x: int = Field(0, validation_alias=AliasChoices("x2", "x3"))


class Inner(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices("a", "b"))


class Outer(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    n: int = Field(alias="N")
    inner: Inner


class Base9(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices("y", "x"))


class Sub9(Base9):
    x: int = Field(0, validation_alias="z")


class AppModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class ChoicesOverAppModel(AppModel):
    x: int = Field(0, validation_alias=AliasChoices("xx"))


class WithDefaults(BaseModel):
    note: str = "n"


class ChoicesOverDefaults(WithDefaults):
    x: int = Field(0, validation_alias=AliasChoices("xx"))


SHAPES = {
    "choices": (Choices, Choices(a=5)),
    "choices_path_first": (ChoicesPathFirst, ChoicesPathFirst(outer=[5])),
    "path": (Path, Path(outer=[5])),
    "deep_path": (DeepPath, DeepPath(a={"b": [0, 5]})),
    "required_path": (RequiredPath, RequiredPath(outer={"x": 5})),
    "mixed": (Mixed, Mixed(yy=1, x2=5)),
    "nested": (Outer, Outer(n=1, inner=Inner(a=5))),
    "config_only_base": (ChoicesOverAppModel, ChoicesOverAppModel.model_validate({"xx": 5})),
    "defaults_only_base": (ChoicesOverDefaults, ChoicesOverDefaults.model_validate({"xx": 5})),
}
RECORDED: dict[str, str] = {}
# Set by the async handler that records last; made anew in each test, in that test's event loop.
ALL_RECORDED = asyncio.Event()


def _dump(model, value) -> str:
    return json.dumps(TypeAdapter(model).dump_python(value, mode="json"), sort_keys=True)


def _service_class() -> type[CliffracerService]:
    namespace = {}
    for name, (model, _) in SHAPES.items():

        def make(name, model):
            async def r(self, item):
                return _dump(model, item)

            r.__name__ = name
            r.__annotations__ = {"item": model, "return": str}

            async def a(self, item):
                RECORDED[name] = _dump(model, item)
                if len(RECORDED) == len(SHAPES):
                    ALL_RECORDED.set()

            a.__name__ = f"{name}_async"
            a.__annotations__ = {"item": model, "return": None}
            return rpc(r), async_rpc(a)

        namespace[name], namespace[f"{name}_async"] = make(name, model)
    return type("Shop", (CliffracerService,), namespace)


class TwoFieldsOneKey(BaseModel):
    """`a` and `b` are both read from `b`, and `p` first from `r`: every dump is refused (a dict
    `p` is read from `b` too), and the validation-alias form is accepted but read with `b`
    holding `a`'s value."""

    a: int = Field(validation_alias="b")
    b: int = Field(validation_alias="b")
    p: dict = Field(validation_alias=AliasChoices("r", "b"))


GOT: list[str] = []


class DeclaresPath(CliffracerService):
    @rpc
    async def path(self, item: TwoFieldsOneKey) -> str:
        GOT.append(repr(item))
        return repr(item)


class ABase(BaseModel):
    x: int = 0


class ASub(ABase):
    x: int = Field(0, validation_alias=AliasChoices("xx"))


class DeclaresABase(CliffracerService):
    @rpc
    async def abase(self, item: ABase) -> int:
        return item.x


class CallerABase(CliffracerService):
    declares = RpcProxy("valias_declares_abase")


class ZBase(BaseModel):
    y: int = 0
    z: int = Field(validation_alias=AliasPath("zz", "k"))


class ZSub(ZBase):
    model_config = ConfigDict(extra="forbid")
    y: int = Field(alias="Y")


class DeclaresZBase(CliffracerService):
    @rpc
    async def zbase(self, item: ZBase) -> int:
        return item.y


class CallerZBase(CliffracerService):
    declares = RpcProxy("valias_declares_zbase")


class Declares9(CliffracerService):
    @rpc
    async def base9(self, item: Base9) -> int:
        return item.x


class Caller9(CliffracerService):
    declares = RpcProxy("valias_declares9")


class Caller(CliffracerService):
    shop = RpcProxy(SERVICE)


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
async def test_each_shape_arrives_with_the_values_passed(fmt):
    global ALL_RECORDED
    RECORDED.clear()
    ALL_RECORDED = asyncio.Event()
    svc = _service_class()(ServiceConfig(name=SERVICE, health_port=0))
    caller = Caller(ServiceConfig(name="valias_caller", health_port=0, serialization_format=fmt))
    await svc.start()
    await caller.start()
    client = ServiceClient(caller.nc, service=SERVICE, verify=False)
    outcomes: dict[str, str] = {}
    try:
        for name, (model, value) in SHAPES.items():
            want = _dump(model, value)
            for how in ("call_rpc", "proxy", "stub"):
                try:
                    if how == "call_rpc":
                        got = await caller.call_rpc(SERVICE, name, item=value)
                    elif how == "proxy":
                        got = await getattr(caller.shop, name)(item=value)
                    else:
                        got = await client._call(name, {"item": client._encode(value, model)}, str)
                    outcomes[f"{name}.{how}"] = "equal" if got == want else f"different {got}"
                except (RpcValidationError, ClientRefused):
                    outcomes[f"{name}.{how}"] = "refused"
            await caller.call_async(SERVICE, f"{name}_async", item=value)
        # Every async handler records once; wait for all of them on the event the last one sets,
        # with a generous ceiling for a loaded host.
        await asyncio.wait_for(ALL_RECORDED.wait(), timeout=10.0)
        for name, (model, value) in SHAPES.items():
            got = RECORDED.get(name)
            outcomes[f"{name}.async"] = (
                "lost"
                if got is None
                else "equal"
                if got == _dump(model, value)
                else f"different {got}"
            )
    finally:
        await caller.stop()
        await svc.stop()

    assert outcomes == dict.fromkeys(outcomes, "equal"), outcomes


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
@pytest.mark.parametrize("how", ["call_rpc", "proxy"])
async def test_a_subclass_to_a_handler_declaring_its_base_keeps_the_dump_the_base_reads(how, fmt):
    """The subclass reads `x` under `z` and the declared base under `x`: the dump `{"x": 5}`,
    which the base reads, is sent, not the subclass's `{"z": 5}`, which the base reads as 0."""
    svc = Declares9(ServiceConfig(name="valias_declares9", health_port=0))
    caller = Caller9(ServiceConfig(name="valias_caller9", health_port=0, serialization_format=fmt))
    await svc.start()
    await caller.start()
    value = Sub9.model_validate({"z": 5})
    try:
        if how == "call_rpc":
            got = await caller.call_rpc("valias_declares9", "base9", item=value)
        else:
            got = await caller.declares.base9(item=value)
    finally:
        await caller.stop()
        await svc.stop()

    assert got == 5


class CallerPath(CliffracerService):
    declares = RpcProxy("valias_declares_path")


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
async def test_a_form_read_as_other_values_is_not_sent_and_the_service_refuses_as_before(fmt):
    """Every dump is refused by the service. The validation-alias form would be accepted and read
    with `b` holding `a`'s value; it is not sent, so the service refuses the call, as on main,
    through every path, and the handler never runs."""
    GOT.clear()
    svc = DeclaresPath(ServiceConfig(name="valias_declares_path", health_port=0))
    caller = CallerPath(
        ServiceConfig(name="valias_caller_path", health_port=0, serialization_format=fmt)
    )
    await svc.start()
    await caller.start()
    client = ServiceClient(caller.nc, service="valias_declares_path", verify=False)
    value = TwoFieldsOneKey.model_validate(
        {"a": 89, "b": 55, "p": {"k": 4}}, by_name=True, by_alias=False
    )
    outcomes = {}
    try:
        for how in ("call_rpc", "proxy", "stub"):
            try:
                if how == "call_rpc":
                    await caller.call_rpc("valias_declares_path", "path", item=value)
                elif how == "proxy":
                    await caller.declares.path(item=value)
                else:
                    await client._call(
                        "path", {"item": client._encode(value, TwoFieldsOneKey)}, str
                    )
                outcomes[how] = "delivered"
            except (RpcValidationError, ClientRefused) as exc:
                outcomes[how] = "refused locally" if "before sending" in str(exc) else "refused"
    finally:
        await caller.stop()
        await svc.stop()

    assert outcomes == dict.fromkeys(("call_rpc", "proxy", "stub"), "refused")
    assert GOT == []


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
@pytest.mark.parametrize("how", ["call_rpc", "proxy"])
async def test_a_subclass_to_a_handler_declaring_a_base_that_reads_by_name_gets_its_value(how, fmt):
    """The base declares `x` with no alias and reads `{"x": 5}`; the subclass reads it under
    `xx`. The dump the base reads is sent, so the handler gets 5, not the default."""
    svc = DeclaresABase(ServiceConfig(name="valias_declares_abase", health_port=0))
    caller = CallerABase(
        ServiceConfig(name="valias_caller_abase", health_port=0, serialization_format=fmt)
    )
    await svc.start()
    await caller.start()
    value = ASub.model_validate({"xx": 5})
    try:
        if how == "call_rpc":
            got = await caller.call_rpc("valias_declares_abase", "abase", item=value)
        else:
            got = await caller.declares.abase(item=value)
    finally:
        await caller.stop()
        await svc.stop()

    assert got == 5


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
@pytest.mark.parametrize("how", ["call_rpc", "proxy"])
async def test_a_base_that_refused_every_dump_still_refuses_rather_than_reading_a_default(how, fmt):
    """ZBase refuses every dump of a ZSub and reads ZSub's validation-alias form with `y=0`. The
    call is refused, as before, and the handler never returns the default."""
    svc = DeclaresZBase(ServiceConfig(name="valias_declares_zbase", health_port=0))
    caller = CallerZBase(
        ServiceConfig(name="valias_caller_zbase", health_port=0, serialization_format=fmt)
    )
    await svc.start()
    await caller.start()
    value = ZSub(Y=2, zz={"k": 9})
    try:
        try:
            if how == "call_rpc":
                outcome = await caller.call_rpc("valias_declares_zbase", "zbase", item=value)
            else:
                outcome = await caller.declares.zbase(item=value)
        except RpcValidationError:
            outcome = "refused"
    finally:
        await caller.stop()
        await svc.stop()

    assert outcome == "refused"
