"""The symptom, through the real listener: an extension named `status` must not decide /health.

The listener chooses 200 or 503 from `body["status"]`. An extension published under the name
`status` replaced that value with its own dict, so a healthy service answered 503 for good. Either
outcome that keeps the endpoint truthful passes: the extension is refused when the service is
built, or it is accepted and the status stays the status string.
"""

import asyncio
import json

import pytest

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig
from cliffracer.core.extension import Extension
from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit


class Contributes(Extension):
    def health_details(self):
        return {"tag": "x"}


async def _get_health(port: int) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body)


async def test_a_healthy_service_with_an_extension_named_status_is_not_pinned_at_503():
    class Svc(CliffracerService):
        status = Contributes()

    try:
        svc = Svc(ServiceConfig(name="svc", health_port=0))
    except ConfigurationError:
        return  # refused where it is declared: the endpoint cannot be corrupted

    await svc.container._setup_extensions()
    svc._running = True
    svc.nc = type(
        "NC",
        (),
        {"is_closed": False, "is_connected": True, "is_draining": False, "is_connecting": False},
    )()
    listener = HealthListener(svc, "127.0.0.1", 0)
    await listener.start()
    try:
        code, body = await _get_health(listener.port)
    finally:
        await listener.stop()

    assert body["status"] == "healthy", body
    assert code == 200
