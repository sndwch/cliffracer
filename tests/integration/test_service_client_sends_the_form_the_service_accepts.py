"""A generated stub's model arguments are accepted by a live service, in the form it takes.

The unit pin is on `_encode`'s return value. This one is the other half: the service
validates the call with the handler's own model under that model's config, so the
claim "the service accepts it" is read from a running service, and the bytes the
service was sent are read off the subject rather than from the client's return value.
"""

import asyncio
import json

import pytest
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import RpcValidationError, ServiceClient

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

SERVICE = "shop_alias_rt"


class AliasOnly(BaseModel):
    item_name: str = Field(alias="itemName")


class Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)
    item_name: str


class Inner(BaseModel):
    inner_name: str = Field(alias="innerName")


class Outer(BaseModel):
    outer_name: str = Field(alias="outerName")
    inner: Inner


class Populatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    item_name: str = Field(alias="itemName")


class ByNameOnly(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    item_name: str = Field(alias="itemName")


class SplitAliases(BaseModel):
    model_config = ConfigDict(validate_by_name=True)
    item_name: str = Field(validation_alias="item_in", serialization_alias="itemOut")


class SerializedByAlias(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True, populate_by_name=True)
    item_name: str = Field(alias="itemName")


class Swapped(BaseModel):
    """Each field's alias is the other's name: the by-name dump is accepted and read swapped."""

    a: str = Field(alias="b")
    b: str = Field(alias="a")


class SwappedPopulatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    a: str = Field(alias="b")
    b: str = Field(alias="a")


class Chained(BaseModel):
    a: str = Field(alias="b")
    b: str = Field(alias="c")
    c: str


class Aliased(BaseModel):
    item_name: str = Field(alias="itemName")


class Plainly(BaseModel):
    item_name: str


class Boxed(BaseModel):
    x: Aliased | Plainly


def _chained(a: str, b: str, c: str) -> Chained:
    """A `Chained` holding exactly these three field values. Its aliases collide with its names,
    so the constructor cannot say them; `model_construct` reads `a` through the alias `b`."""
    value = Chained.model_construct(**{"b": a, "c": b})
    value.c = c
    return value


class Shop(CliffracerService):
    @rpc
    async def alias_only(self, item: AliasOnly) -> str:
        return item.item_name

    @rpc
    async def camel(self, item: Camel) -> str:
        return item.item_name

    @rpc
    async def nested(self, item: Outer) -> str:
        return f"{item.outer_name}/{item.inner.inner_name}"

    @rpc
    async def listed(self, items: list[AliasOnly]) -> str:
        return ",".join(i.item_name for i in items)

    @rpc
    async def populatable(self, item: Populatable) -> str:
        return item.item_name

    @rpc
    async def by_name_only(self, item: ByNameOnly) -> str:
        return item.item_name

    @rpc
    async def split_aliases(self, item: SplitAliases) -> str:
        return item.item_name

    @rpc
    async def serialized_by_alias(self, item: SerializedByAlias) -> str:
        return item.item_name

    @rpc
    async def swapped(self, item: Swapped) -> str:
        return f"{item.a}/{item.b}"

    @rpc
    async def swapped_populatable(self, item: SwappedPopulatable) -> str:
        return f"{item.a}/{item.b}"

    @rpc
    async def chained(self, item: Chained) -> str:
        return f"{item.a}/{item.b}/{item.c}"

    @rpc
    async def boxed(self, item: Boxed) -> str:
        return f"{type(item.x).__name__}:{item.x.item_name}"


class ShopClient(ServiceClient):
    """What the generator emits: `self._encode(argument, annotation)` into `_call`."""

    SERVICE = SERVICE

    async def alias_only(self, item: AliasOnly) -> str:
        return await self._call("alias_only", {"item": self._encode(item, AliasOnly)}, str)

    async def camel(self, item: Camel) -> str:
        return await self._call("camel", {"item": self._encode(item, Camel)}, str)

    async def nested(self, item: Outer) -> str:
        return await self._call("nested", {"item": self._encode(item, Outer)}, str)

    async def listed(self, items: list[AliasOnly]) -> str:
        return await self._call("listed", {"items": self._encode(items, list[AliasOnly])}, str)

    async def populatable(self, item: Populatable) -> str:
        return await self._call("populatable", {"item": self._encode(item, Populatable)}, str)

    async def by_name_only(self, item: ByNameOnly) -> str:
        return await self._call("by_name_only", {"item": self._encode(item, ByNameOnly)}, str)

    async def split_aliases(self, item: SplitAliases) -> str:
        return await self._call("split_aliases", {"item": self._encode(item, SplitAliases)}, str)

    async def serialized_by_alias(self, item: SerializedByAlias) -> str:
        return await self._call(
            "serialized_by_alias", {"item": self._encode(item, SerializedByAlias)}, str
        )

    async def swapped(self, item: Swapped) -> str:
        return await self._call("swapped", {"item": self._encode(item, Swapped)}, str)

    async def swapped_populatable(self, item: SwappedPopulatable) -> str:
        return await self._call(
            "swapped_populatable", {"item": self._encode(item, SwappedPopulatable)}, str
        )

    async def chained(self, item: Chained) -> str:
        return await self._call("chained", {"item": self._encode(item, Chained)}, str)

    async def boxed(self, item: Boxed) -> str:
        return await self._call("boxed", {"item": self._encode(item, Boxed)}, str)


# Seconds to wait for the observer's copy of a request, or for a handler with no reply to run.
# Generous, because a loaded host delays delivery; a run that passes waits only as long as it takes.
OBSERVER_TIMEOUT = 10.0
# Seconds to wait after the copy arrives before counting copies, so a second one would be counted.
SETTLE = 0.1


async def _call_and_capture(nats_connection, method, *args):
    """Call `method` through a stub and return (the reply, the JSON body the service was sent).

    The observer subscribes, and is flushed to the broker, before the service starts, and the call
    waits on its callback rather than polling for a copy.
    """
    svc = Shop(ServiceConfig(name=SERVICE, version="1.0.0"))
    client = ShopClient(nats_connection, service=SERVICE, verify=False)
    seen: list[bytes] = []
    arrived = asyncio.Event()

    async def record(msg):
        seen.append(msg.data)
        arrived.set()

    sub = await nats_connection.subscribe(client._subject(f"rpc.{method}"), cb=record)
    await nats_connection.flush()
    await svc.start()
    try:
        try:
            reply = await getattr(client, method)(*args)
        except RpcValidationError as exc:
            reply = exc
        await asyncio.wait_for(arrived.wait(), timeout=OBSERVER_TIMEOUT)
        await asyncio.sleep(SETTLE)
        assert len(seen) == 1, seen
        return reply, json.loads(seen[0])
    finally:
        await sub.unsubscribe()
        await svc.stop()


@pytest.mark.parametrize(
    ("method", "args", "reply", "wire"),
    [
        pytest.param(
            "alias_only",
            (AliasOnly(itemName="x"),),
            "x",
            {"item": {"itemName": "x"}},
            id="alias-only",
        ),
        pytest.param(
            "camel", (Camel(itemName="x"),), "x", {"item": {"itemName": "x"}}, id="generator"
        ),
        pytest.param(
            "nested",
            (Outer(outerName="o", inner=Inner(innerName="i")),),
            "o/i",
            {"item": {"outerName": "o", "inner": {"innerName": "i"}}},
            id="nested",
        ),
        pytest.param(
            "listed",
            ([AliasOnly(itemName="a"), AliasOnly(itemName="b")],),
            "a,b",
            {"items": [{"itemName": "a"}, {"itemName": "b"}]},
            id="list",
        ),
    ],
)
async def test_a_call_the_service_refused_by_field_name_now_succeeds(
    nats_connection, method, args, reply, wire
):
    got, sent = await _call_and_capture(nats_connection, method, *args)

    assert got == reply
    assert sent == wire


@pytest.mark.parametrize(
    ("method", "args", "wire"),
    [
        pytest.param(
            "populatable",
            (Populatable(itemName="x"),),
            {"item": {"item_name": "x"}},
            id="populatable",
        ),
        pytest.param(
            "by_name_only",
            (ByNameOnly(item_name="x"),),
            {"item": {"item_name": "x"}},
            id="by-name-only",
        ),
        pytest.param(
            "split_aliases",
            (SplitAliases(item_name="x"),),
            {"item": {"item_name": "x"}},
            id="split-aliases",
        ),
        pytest.param(
            "serialized_by_alias",
            (SerializedByAlias(itemName="x"),),
            {"item": {"itemName": "x"}},
            id="serialized-by-alias",
        ),
    ],
)
async def test_a_call_the_service_accepted_is_sent_the_bytes_it_was_sent_before(
    nats_connection, method, args, wire
):
    """`by_alias=True` here would send `itemName` for `populatable` and a refused `itemOut`
    for `split_aliases`; `by_name_only` would be refused."""
    got, sent = await _call_and_capture(nats_connection, method, *args)

    assert got == "x"
    assert sent == wire


async def test_a_model_the_service_would_read_swapped_is_read_as_passed(nats_connection):
    """The by-name dump `{"a": "1", "b": "2"}` is accepted by the service and read as
    a="2", b="1". The reply is what the service read."""
    got, sent = await _call_and_capture(nats_connection, "swapped", Swapped(b="1", a="2"))

    assert got == "1/2"
    assert sent == {"item": {"b": "1", "a": "2"}}


@pytest.mark.parametrize(
    ("method", "arg", "reply", "wire"),
    [
        pytest.param(
            "swapped_populatable",
            SwappedPopulatable(b="1", a="2"),
            "1/2",
            {"b": "1", "a": "2"},
            id="swapped-populatable",
        ),
        pytest.param(
            "chained",
            _chained("A", "B", "B"),
            "A/B/B",
            {"b": "A", "c": "B"},
            id="chain",
        ),
        pytest.param(
            "boxed",
            Boxed(x=Aliased(itemName="p")),
            "Aliased:p",
            {"x": {"itemName": "p"}},
            id="union-member",
        ),
    ],
)
async def test_a_form_the_service_would_read_as_something_else_is_not_sent(
    nats_connection, method, arg, reply, wire
):
    """Each of these is accepted by the service in its by-name dump and read as another value
    (the other class for the union), so the service's reply is the evidence."""
    got, sent = await _call_and_capture(nats_connection, method, arg)

    assert got == reply
    assert sent == {"item": wire}


async def test_a_chain_of_aliases_with_distinct_values_is_refused_before_sending(nats_connection):
    """No form reads back as the argument, and the plain dump would be read shifted (`a` holding
    `b`'s value, `b` holding `c`'s), so the stub refuses the call, naming both fields, and nothing
    reaches the service."""
    svc = Shop(ServiceConfig(name=SERVICE, version="1.0.0"))
    await svc.start()
    client = ShopClient(nats_connection, service=SERVICE, verify=False)
    seen: list[bytes] = []

    async def record(msg):
        seen.append(msg.data)

    sub = await nats_connection.subscribe(client._subject("rpc.chained"), cb=record)
    await nats_connection.flush()
    try:
        with pytest.raises(RpcValidationError) as refused:
            await client.chained(_chained("1", "2", "3"))
        await nats_connection.flush()
        await asyncio.sleep(0.1)
    finally:
        await sub.unsubscribe()
        await svc.stop()

    assert "refused before sending" in str(refused.value)
    assert {(d["type"], d["loc"][0]) for d in refused.value.details} == {
        ("value_would_be_misread", "a"),
        ("value_would_be_misread", "b"),
    }
    assert seen == []
