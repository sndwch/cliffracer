"""Integration tests for ServiceClient RPC communication and schema verification."""

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import (
    ClientOutOfDate,
    RpcNoResponders,
    RpcRefused,
    RpcUnknownMethod,
    RpcValidationError,
    ServiceClient,
)
from cliffracer.introspect import describe

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


class Item(BaseModel):
    sku: str
    qty: int = 1


class Receipt(BaseModel):
    sku: str
    total: int


class Shop(CliffracerService):
    @rpc
    async def buy(self, item: Item, gift: bool = False) -> Receipt:
        """Buy one item."""
        return Receipt(sku=item.sku, total=item.qty * (0 if gift else 10))


def _client_class(signatures: dict[str, str]) -> type[ServiceClient]:
    """Construct a test ServiceClient stub class with given signatures."""

    class ShopClient(ServiceClient):
        SERVICE = "shop_rt"
        VERSION = "1.0.0"
        DESCRIPTION_HASH = "sha256:unused-here"
        SIGNATURES = signatures

        async def buy(self, item: Item, gift: bool = False) -> Receipt:
            return await self._call(
                "buy",
                {"item": self._encode(item, Item), "gift": self._encode(gift, bool)},
                Receipt,
            )

    return ShopClient


def _signatures() -> dict[str, str]:
    desc = describe(Shop, service="shop_rt", version="1.0.0")
    return {m.name: m.signature_hash for m in desc.methods}


async def test_a_generated_client_calls_a_real_service(nats_connection):
    svc = Shop(ServiceConfig(name="shop_rt", version="1.0.0"))
    await svc.start()
    client = _client_class(_signatures())(nats_connection, service="shop_rt")
    try:
        assert await client.buy(Item(sku="a", qty=3)) == Receipt(sku="a", total=30)
        assert await client.buy(Item(sku="b"), gift=True) == Receipt(sku="b", total=0)
    finally:
        await svc.stop()


async def test_verify_reads_the_live_describe_subject(nats_connection):
    """The signatures come from `describe(Shop)`; the check reads the SERVICE.

    Both sides compute the hash with the same function over the same class, so
    a passing call here is the offline and online descriptions agreeing over
    the wire rather than in one process.
    """
    svc = Shop(ServiceConfig(name="shop_rt", version="1.0.0"))
    await svc.start()
    client = _client_class(_signatures())(nats_connection, service="shop_rt")
    try:
        await client.verify()
    finally:
        await svc.stop()


async def test_a_stale_signature_raises_ClientOutOfDate_naming_the_method(nats_connection):
    """What the whole hash apparatus is for: the client is told, by name."""
    svc = Shop(ServiceConfig(name="shop_rt", version="1.0.0"))
    await svc.start()
    stale = dict(_signatures(), buy="sha256:what-it-looked-like-last-week")
    client = _client_class(stale)(nats_connection, service="shop_rt")
    try:
        with pytest.raises(ClientOutOfDate) as caught:
            await client.buy(Item(sku="a"))
        assert caught.value.changed == ["buy"]
        assert caught.value.missing == []
        assert "regenerate" in str(caught.value)
    finally:
        await svc.stop()


async def test_a_bad_argument_comes_back_as_RpcValidationError_with_pydantics_loc(nats_connection):
    svc = Shop(ServiceConfig(name="shop_rt", version="1.0.0"))
    await svc.start()
    client = _client_class(_signatures())(nats_connection, service="shop_rt")
    try:
        with pytest.raises(RpcValidationError) as caught:
            # Straight through _call, because a typed stub would not let this
            # argument past the annotation on the way out.
            await client._call("buy", {"item": {"sku": "a", "qty": "many"}}, Receipt)
        locs = [e.get("loc") for e in caught.value.details]
        assert ["item", "qty"] in locs, caught.value.details
    finally:
        await svc.stop()


async def test_a_refused_describe_arrives_as_RpcRefused_over_the_wire(nats_connection):
    """The fixture version of this uses the container's bytes through a fake
    connection; this one puts them on a broker. An extension refusing a
    describe is the reason that subject runs through the hook chain at all, so
    the refusal is worth proving end to end."""
    from cliffracer.core.extension import Extension, RejectMessage

    class Deny(Extension):
        async def worker_setup(self, ctx):
            if ctx.kind == "describe":
                raise RejectMessage("unauthenticated")

    class GuardedShop(Shop):
        deny = Deny()

    svc = GuardedShop(ServiceConfig(name="shop_rt", version="1.0.0"))
    await svc.start()
    client = _client_class(_signatures())(nats_connection, service="shop_rt")
    try:
        with pytest.raises(RpcRefused) as caught:
            await client.buy(Item(sku="a"))
        assert caught.value.reason == "unauthenticated"
    finally:
        await svc.stop()


async def test_a_stopped_service_is_RpcNoResponders_not_a_timeout(nats_connection):
    """Immediately, and by its own name. A client that waited out the full
    deadline for a service nobody is running would report a slow service."""
    client = _client_class(_signatures())(nats_connection, service="shop_rt_absent", timeout=5.0)
    with pytest.raises(RpcNoResponders):
        await client.buy(Item(sku="a"))


async def test_a_method_the_service_does_not_have_is_RpcUnknownMethod(nats_connection):
    svc = Shop(ServiceConfig(name="shop_rt", version="1.0.0"))
    await svc.start()
    client = _client_class({})(nats_connection, service="shop_rt")
    try:
        with pytest.raises(RpcUnknownMethod):
            await client._call("refund", {}, Receipt)
    finally:
        await svc.stop()
