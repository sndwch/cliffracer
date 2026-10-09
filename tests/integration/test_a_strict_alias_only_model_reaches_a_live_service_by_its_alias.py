"""A strict model readable only by its alias is sent by it, and a live service accepts the call.

The client chose the wire form with python-mode validation, which a strict model refuses for the JSON
form of its datetime. Both spellings were refused, the by-name dump went out as the fallback, and the
service answered "field required". The bytes the service is sent are read off the subject.
"""

import asyncio
import datetime
import json

import pytest
from pydantic import BaseModel, ConfigDict, Field

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import ServiceClient

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

SERVICE = "strict_alias_e2e"
WHEN = datetime.datetime(2026, 1, 2, 3, 4, 5)


class StrictAlias(BaseModel):
    model_config = ConfigDict(strict=True)

    when: datetime.datetime = Field(alias="When")


class Shop(CliffracerService):
    @rpc
    async def put(self, item: StrictAlias) -> str:
        return item.when.isoformat()

    @rpc
    async def put_all(self, items: list[StrictAlias]) -> int:
        return len(items)


class ShopClient(ServiceClient):
    SERVICE = SERVICE

    async def put(self, item: StrictAlias) -> str:
        return await self._call("put", {"item": self._encode(item, StrictAlias)}, str)

    async def put_all(self, items: list[StrictAlias]) -> int:
        return await self._call("put_all", {"items": self._encode(items, list[StrictAlias])}, int)


# Seconds to wait for the observer's copy of a request, or for a handler with no reply to run.
# Generous, because a loaded host delays delivery; a run that passes waits only as long as it takes.
OBSERVER_TIMEOUT = 10.0
# Seconds to wait after the copy arrives before counting copies, so a second one would be counted.
SETTLE = 0.1


async def _call_and_capture(nats_connection, method, *args):
    """Call `method` through a stub; return (the reply, the JSON body the service was sent).

    The observer subscribes, and is flushed to the broker, before the service starts, and the call
    waits on its callback rather than polling for a copy.
    """
    service = Shop(ServiceConfig(name=SERVICE, version="1.0.0"))
    client = ShopClient(nats_connection, service=SERVICE, verify=False)
    seen: list[bytes] = []
    arrived = asyncio.Event()

    async def record(msg):
        seen.append(msg.data)
        arrived.set()

    sub = await nats_connection.subscribe(client._subject(f"rpc.{method}"), cb=record)
    await nats_connection.flush()
    await service.start()
    try:
        reply = await getattr(client, method)(*args)
        await asyncio.wait_for(arrived.wait(), timeout=OBSERVER_TIMEOUT)
        await asyncio.sleep(SETTLE)
        assert len(seen) == 1, seen
        return reply, json.loads(seen[0])
    finally:
        await sub.unsubscribe()
        await service.stop()


async def test_a_strict_alias_only_model_is_accepted_by_a_live_service(nats_connection):
    reply, wire = await _call_and_capture(nats_connection, "put", StrictAlias(When=WHEN))

    assert reply == "2026-01-02T03:04:05"
    assert wire == {"item": {"When": "2026-01-02T03:04:05"}}


async def test_a_list_of_them_is_accepted_by_a_live_service(nats_connection):
    reply, wire = await _call_and_capture(
        nats_connection, "put_all", [StrictAlias(When=WHEN), StrictAlias(When=WHEN)]
    )

    assert reply == 2
    assert wire == {"items": [{"When": "2026-01-02T03:04:05"}] * 2}
