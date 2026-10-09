"""On a live broker, a broadcast handler registered before start receives its broadcast, and one
registered after start is refused by name instead of silently never being called."""

import asyncio
import os

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import ServiceLifecycleError

pytestmark = pytest.mark.integration

BROKER_ENV = "CLIFFRACER_TEST_NATS_URL"


@pytest.mark.nats_required
async def test_before_start_delivers_and_after_start_refuses() -> None:
    url = os.getenv(BROKER_ENV)
    if not url:
        pytest.skip(f"${BROKER_ENV} is not set; there is no broker to check against")
    seen: list[str] = []

    async def early(**payload) -> None:
        seen.append("early")

    async def late(**payload) -> None:
        seen.append("late")

    svc = CliffracerService(
        ServiceConfig(name="broadcast_late", nats_url=url, health_port=0, auto_restart=False)
    )
    svc.register_broadcast_handler("things.created", early)
    await svc.start()
    try:
        with pytest.raises(ServiceLifecycleError, match="things.updated"):
            svc.register_broadcast_handler("things.updated", late)

        await svc.broadcast_message("things.created", x=1)
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.05)
    finally:
        await svc.stop()

    assert seen == ["early"], seen
