"""The warning for an undecorated override is bound to the service whose class has it.

A NATS log sink publishes only the records bound to its service. Discovery wrote the warning that
a subclass's override of a decorated handler registers nothing through the bare loguru logger, so
the one line that says a handler silently went away reached no `logs.<service>.<level>` stream.
It is written through a logger bound to the service being discovered.

The sink here is attached by hand before discovery. In a running service discovery runs before the
connection, and `LoggingExtension` attaches its sink after it, so the warning reaches a stream there
only through a sink the host attached earlier.
"""

import asyncio

import pytest
from cliffracer_logging import LoggingConfig
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.testing import wait_until

pytestmark = pytest.mark.unit


class RecordingNats:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.messages.append(f"{subject} {payload.decode()}")


@pytest.fixture(autouse=True)
def isolated_logger():
    logger.remove()
    yield
    logger.remove()


def _two_services_streaming() -> tuple[RecordingNats, RecordingNats]:
    """A sink for `orders` and one for `billing`, each filtered to its own service."""
    orders, billing = RecordingNats(), RecordingNats()
    for name, nc in (("orders", orders), ("billing", billing)):
        config = ServiceConfig(name=name, subject_prefix=None, health_port=0)
        LoggingConfig.add_nats_sink(name, nc, config=config, log_level="DEBUG")
    return orders, billing


async def _reaches_orders_and_not_billing(
    marker: str, orders: RecordingNats, billing: RecordingNats
) -> None:
    """The line is in `orders`' sink, and, once everything queued has been sent, not in `billing`'s."""
    await wait_until(
        lambda: (logger.complete(), any(marker in m for m in orders.messages))[1],
        within=5.0,
        reason=f"{marker!r} on orders",
    )
    await asyncio.sleep(0.2)
    logger.complete()
    assert not [m for m in billing.messages if marker in m], billing.messages


class Base(CliffracerService):
    @rpc
    async def go(self) -> int:
        return 1


class Overrides(Base):
    async def go(self) -> int:  # not decorated: the base's handler is gone
        return 2


async def test_the_override_warning_is_in_its_services_stream_and_in_no_other():
    orders, billing = _two_services_streaming()
    config = ServiceConfig(name="orders", subject_prefix=None, health_port=0)
    service = Overrides(config)

    HandlerDiscovery.discover(service, config)

    await _reaches_orders_and_not_billing("overrides Base.go", orders, billing)
