"""`register_broadcast_handler` after the service started raises, instead of registering a handler
that no subscription will ever deliver to.

Subscriptions are created once, during `start()`. A registration after that was accepted, listed
in the registry, and never called.
"""

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import ServiceLifecycleError

pytestmark = pytest.mark.unit


class Svc(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="late", health_port=0, subject_prefix=None))
        self.seen: list[str] = []


async def _handler(**payload) -> None:  # pragma: no cover - never delivered in these tests
    return None


async def test_a_registration_before_start_is_accepted():
    svc = Svc()

    svc.register_broadcast_handler("things.created", _handler)

    assert list(svc.container.registry.broadcast_handlers) == ["things.created"]


async def test_a_registration_after_start_is_refused_with_the_reason():
    svc = Svc()
    svc.container.lifecycle._running = True  # what start() leaves behind

    with pytest.raises(ServiceLifecycleError) as caught:
        svc.register_broadcast_handler("things.created", _handler)

    message = str(caught.value)
    assert "things.created" in message and "late" in message and "on_startup" in message
    assert not svc.container.registry.broadcast_handlers


async def test_a_registration_from_on_startup_is_accepted():
    """`on_startup` runs before the subscriptions are created, so it is the late place."""

    class Registering(Svc):
        async def on_startup(self) -> None:
            self.register_broadcast_handler("things.created", _handler)

    svc = Registering()
    svc.container.lifecycle._running = False

    await svc.on_startup()

    assert list(svc.container.registry.broadcast_handlers) == ["things.created"]
    await asyncio.sleep(0)
