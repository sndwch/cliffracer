"""A tree no whole-value form satisfies is accepted by a live service, through both client paths.

The outer model is read by field name and holds an inner model read by alias. A generated stub
(`ServiceClient._encode`) and `call_rpc` (`RpcProxy`) both send it one level at a time. With a
serializer on the way the call goes out as before and the service refuses it. The bytes the service
was sent are read off the subject.
"""

import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field, field_serializer

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import RpcValidationError, ServiceClient
from cliffracer.core.discovery import HandlerDiscovery

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

SERVICE = "shop_nested_rt"


class Inner(BaseModel):
    inner_name: str = Field(alias="innerName")


class OuterByName(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    outer_name: str = Field(alias="outerName")
    inner: Inner


class Leaf(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    leaf_name: str = Field(alias="leafName")


class Middle(BaseModel):
    middle: Leaf = Field(alias="middleLeaf")


class Top(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    top: Middle


class Serialized(OuterByName):
    @field_serializer("outer_name")
    def _unchanged(self, value: str) -> str:
        return value


class StrictByNameInner(BaseModel):
    model_config = ConfigDict(strict=True, validate_by_alias=False, validate_by_name=True)
    when: datetime = Field(alias="When")
    ident: UUID = Field(alias="Ident")


class AliasOuterOverStrictInner(BaseModel):
    outer_name: str = Field(alias="outerName")
    inner: StrictByNameInner


class StrictAliasOuterOverStrictInner(BaseModel):
    model_config = ConfigDict(strict=True)
    outer_name: str = Field(alias="outerName")
    inner: StrictByNameInner


WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
IDENT = UUID("12345678-1234-5678-1234-567812345678")


class Shop(CliffracerService):
    @rpc
    async def outer(self, item: OuterByName) -> str:
        return f"{item.outer_name}/{item.inner.inner_name}"

    @rpc
    async def tree(self, item: Top) -> str:
        return item.top.middle.leaf_name

    @rpc
    async def serialized(self, item: Serialized) -> str:
        return item.outer_name

    @rpc
    async def strict_inner(self, item: AliasOuterOverStrictInner) -> str:
        return f"{item.outer_name}/{item.inner.when.isoformat()}/{item.inner.ident}"

    @rpc
    async def strict_both(self, item: StrictAliasOuterOverStrictInner) -> str:
        return f"{item.outer_name}/{item.inner.when.isoformat()}/{item.inner.ident}"


class ShopClient(ServiceClient):
    SERVICE = SERVICE

    async def outer(self, item: OuterByName) -> str:
        return await self._call("outer", {"item": self._encode(item, OuterByName)}, str)

    async def tree(self, item: Top) -> str:
        return await self._call("tree", {"item": self._encode(item, Top)}, str)

    async def serialized(self, item: Serialized) -> str:
        return await self._call("serialized", {"item": self._encode(item, Serialized)}, str)

    async def strict_inner(self, item: AliasOuterOverStrictInner) -> str:
        return await self._call(
            "strict_inner", {"item": self._encode(item, AliasOuterOverStrictInner)}, str
        )

    async def strict_both(self, item: StrictAliasOuterOverStrictInner) -> str:
        return await self._call(
            "strict_both", {"item": self._encode(item, StrictAliasOuterOverStrictInner)}, str
        )


# Seconds to wait for the observer's copy of a request: generous, because a loaded host delays its
# delivery, and a run that passes waits only as long as the copy takes.
OBSERVER_TIMEOUT = 10.0
# Seconds to wait after the copy arrives before counting copies, so a second one would be counted.
SETTLE = 0.1


async def _send(nats_connection, how, method, item):
    """Send `item` to `method` by a stub (`how="stub"`) or by `call_rpc`; return (reply, JSON sent).

    The observer subscribes, and is flushed to the broker, before either service starts, and the
    call waits on its callback rather than polling for a copy.
    """
    svc = Shop(ServiceConfig(name=SERVICE, version="1.0.0"))
    caller = CliffracerService(ServiceConfig(name="caller_nested_rt", version="1.0.0"))
    client = ShopClient(nats_connection, service=SERVICE, verify=False)
    subject = HandlerDiscovery.outbound_subject(caller.config, SERVICE, "rpc", method)
    seen: list[bytes] = []
    arrived = asyncio.Event()

    async def record(msg):
        seen.append(msg.data)
        arrived.set()

    sub = await nats_connection.subscribe(subject, cb=record)
    await nats_connection.flush()
    await svc.start()
    await caller.start()
    try:
        try:
            if how == "stub":
                reply = await getattr(client, method)(item)
            else:
                reply = await caller.call_rpc(SERVICE, method, item=item)
        except RpcValidationError as exc:
            reply = exc
        await asyncio.wait_for(arrived.wait(), timeout=OBSERVER_TIMEOUT)
        await asyncio.sleep(SETTLE)
        assert len(seen) == 1, seen
        body = json.loads(seen[0])
        return reply, body["item"]
    finally:
        await sub.unsubscribe()
        await caller.stop()
        await svc.stop()


@pytest.mark.parametrize("how", ["stub", "call_rpc"])
async def test_a_tree_no_whole_value_form_satisfies_is_accepted(nats_connection, how):
    got, sent = await _send(
        nats_connection,
        how,
        "outer",
        OuterByName(outer_name="o", inner=Inner(innerName="i")),
    )

    assert got == "o/i"
    assert sent == {"outer_name": "o", "inner": {"innerName": "i"}}


@pytest.mark.parametrize("how", ["stub", "call_rpc"])
async def test_a_tree_whose_every_level_needs_a_different_form_is_accepted(nats_connection, how):
    got, sent = await _send(
        nats_connection, how, "tree", Top(top=Middle(middleLeaf=Leaf(leaf_name="l")))
    )

    assert got == "l"
    assert sent == {"top": {"middleLeaf": {"leaf_name": "l"}}}


@pytest.mark.parametrize(
    ("how", "main_form"),
    [
        pytest.param("stub", {"outer_name": "o", "inner": {"inner_name": "i"}}, id="stub"),
        pytest.param("call_rpc", {"outerName": "o", "inner": {"innerName": "i"}}, id="call_rpc"),
    ],
)
async def test_a_serializer_on_the_way_sends_the_call_as_before_and_it_is_refused(
    nats_connection, how, main_form
):
    got, sent = await _send(
        nats_connection,
        how,
        "serialized",
        Serialized(outer_name="o", inner=Inner(innerName="i")),
    )

    assert isinstance(got, RpcValidationError), got
    assert sent == main_form


@pytest.mark.parametrize("how", ["stub", "call_rpc"])
@pytest.mark.parametrize(
    ("method", "item"),
    [
        pytest.param(
            "strict_inner",
            AliasOuterOverStrictInner(
                outerName="o", inner=StrictByNameInner(when=WHEN, ident=IDENT)
            ),
            id="strict-by-name-inner-in-an-alias-outer",
        ),
        pytest.param(
            "strict_both",
            StrictAliasOuterOverStrictInner(
                outerName="o", inner=StrictByNameInner(when=WHEN, ident=IDENT)
            ),
            id="strict-alias-outer-over-a-strict-by-name-inner",
        ),
    ],
)
async def test_a_strict_level_is_written_in_the_form_the_service_reads(
    nats_connection, how, method, item
):
    """The strict inner level refuses its datetime and UUID text in python mode and reads them in
    JSON mode, as the service does, so it is written by field name inside an outer written by alias."""
    got, sent = await _send(nats_connection, how, method, item)

    assert got == f"o/{WHEN.isoformat()}/{IDENT}"
    assert sent == {
        "outerName": "o",
        "inner": {"when": "2026-01-02T03:04:05Z", "ident": str(IDENT)},
    }
