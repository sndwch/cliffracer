"""A "shutdown" RPC that starts the stop and returns gets its reply out, and the service then stops.

`await self.stop()` inside a handler returns promptly (the drain leaves the task running the stop
alone) but the stop has disconnected the service, so the handler cannot reply and its caller's
request times out. The documented pattern is `self._stopping = asyncio.create_task(self.stop())` and
return: the reply is sent before the connection goes, and the reference keeps the task from being
garbage-collected before it finishes (the loop holds a task only weakly). This pins that pattern on
a live broker.
"""

import asyncio
import json

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.discovery import HandlerDiscovery

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


class Quitter(CliffracerService):
    def __init__(self) -> None:
        super().__init__(
            ServiceConfig(name="shutdown_reply_live", health_port=0, shutdown_timeout=2.0)
        )
        self._stopping: asyncio.Task[None] | None = None

    @rpc
    async def shutdown(self) -> dict[str, bool]:
        self._stopping = asyncio.create_task(self.stop())
        return {"stopping": True}


async def test_the_caller_gets_its_reply_and_the_service_stops(nats_connection):
    service = Quitter()
    await service.start()
    subject = HandlerDiscovery.with_namespace(service.config, "shutdown_reply_live.rpc.shutdown")

    reply = await nats_connection.request(subject, b"{}", timeout=5)

    assert json.loads(reply.data)["result"] == {"stopping": True}
    assert service._stopping is not None
    await asyncio.wait_for(service._stopping, timeout=10)
    assert service.container.lifecycle.is_stopped
