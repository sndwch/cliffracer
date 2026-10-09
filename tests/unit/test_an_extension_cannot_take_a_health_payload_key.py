"""An extension is published under its own name, so it may not be named for a payload key.

`health_check` and `get_service_info` write each extension's contribution at the top level of
the payload under the extension's name. An extension called `status` therefore replaced the
status string with its own dict, and the health listener, which reads `body["status"]` to choose
200 or 503, answered 503 for a healthy service for good. The collision is refused where the
extension is bound.
"""

import asyncio
import json

import pytest

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig
from cliffracer.core.dependencies import Dependency
from cliffracer.core.extension import RESERVED_PAYLOAD_KEYS, Extension
from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit


class Contributes(Extension):
    def health_details(self):
        return {"tag": "x"}

    def info_details(self):
        return {"tag": "x"}


def _service_with_extension_named(name: str) -> CliffracerService:
    cls = type("Svc", (CliffracerService,), {name: Contributes()})
    return cls(ServiceConfig(name="svc", health_port=0))


@pytest.mark.parametrize("name", sorted(RESERVED_PAYLOAD_KEYS))
def test_an_extension_named_for_a_payload_key_is_refused_at_construction(name):
    with pytest.raises(ConfigurationError) as refused:
        _service_with_extension_named(name)

    assert repr(name) in str(refused.value)


def test_an_extension_added_under_a_payload_key_is_refused_too():
    svc = CliffracerService(ServiceConfig(name="svc", health_port=0))

    with pytest.raises(ConfigurationError, match="'status'"):
        svc.add_extension(Contributes(), name="status")


async def test_CONTROL_an_ordinarily_named_extension_still_publishes_under_its_name():
    """The refusal is about the name, not about contributing: `status` stays the status string."""
    svc = _service_with_extension_named("pool")
    await svc.container._setup_extensions()
    svc._running = True

    health = await svc.health_check()

    assert health["pool"] == {"tag": "x"}
    assert isinstance(health["status"], str)
    assert svc.get_service_info()["pool"] == {"tag": "x"}


async def test_CONTROL_a_name_with_a_leading_underscore_is_not_published_so_it_is_accepted():
    svc = _service_with_extension_named("_status")
    await svc.container._setup_extensions()

    health = await svc.health_check()

    assert "_status" not in health
    assert isinstance(health["status"], str)


async def test_the_reserved_set_covers_every_key_the_payloads_carry():
    """Drift guard: a key added to a payload without being added to the set would be open to the
    same collision. Read from real payloads, with a failing dependency so those keys appear."""

    async def refuses() -> None:
        raise RuntimeError("down")

    svc = CliffracerService(ServiceConfig(name="svc", health_port=0))
    svc._dependencies.append(Dependency(name="db", probe=refuses, timeout=1.0))
    await svc.container._setup_extensions()
    svc._running = True

    health_keys = set(await svc.health_check())
    info_keys = set(svc.get_service_info())

    assert {"dependencies", "unhealthy_dependencies"} <= health_keys, health_keys
    assert (health_keys | info_keys) <= RESERVED_PAYLOAD_KEYS, (
        health_keys | info_keys
    ) - RESERVED_PAYLOAD_KEYS


async def test_the_reserved_set_covers_every_key_the_served_info_body_carries():
    """The listener adds `health_port` to /info after `get_service_info()` returns, so a guard that
    reads only `get_service_info()` misses it. Read the body the listener actually serves."""
    svc = CliffracerService(ServiceConfig(name="svc", health_port=0))
    svc._running = True
    listener = HealthListener(svc, "127.0.0.1", 0)
    await listener.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        writer.write(b"GET /info HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        raw = await reader.read()
        writer.close()
    finally:
        await listener.stop()
    served = set(json.loads(raw.partition(b"\r\n\r\n")[2]))

    assert "health_port" in served, served  # the premise: the listener does add it
    assert served <= RESERVED_PAYLOAD_KEYS, served - RESERVED_PAYLOAD_KEYS
