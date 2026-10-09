"""The connection pool's lines carry the service it was built for, so that service's stream has them.

A NATS log sink publishes only the records bound to its service. The pool wrote through the bare
loguru logger, so "pool ready", a connection that failed and the close it logged reached no
`logs.<service>.<level>` stream. `PoolExtension` names the pool for its service, and the pool
writes through a logger bound to that name; a pool built with no name (standalone use) has no
service to bind.
"""

import asyncio

import pytest
from cliffracer_logging import LoggingConfig
from cliffracer_metrics import OptimizedNATSConnection
from loguru import logger

from cliffracer import ServiceConfig
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


def _pool(name: str | None) -> OptimizedNATSConnection:
    return OptimizedNATSConnection(nats_url="nats://broker.example:5222", name=name)


async def test_the_lines_a_pool_writes_when_it_closes_are_in_its_services_stream():
    orders, billing = _two_services_streaming()

    await _pool("orders").close()

    await _reaches_orders_and_not_billing("Closing optimized connection pool", orders, billing)
    await _reaches_orders_and_not_billing("Optimized connection pool closed", orders, billing)


def test_the_pool_error_callback_line_carries_its_service():
    seen: list[dict] = []
    logger.add(lambda message: seen.append(message.record["extra"]), level="ERROR")
    pool = _pool("orders")

    pool._log.error("a pooled connection reported an error")
    logger.complete()

    assert [extra.get("service") for extra in seen] == ["orders"]


async def test_the_line_a_pooled_connections_error_writes_is_in_its_services_stream():
    orders, billing = _two_services_streaming()

    await _pool("orders")._error_callback(1)(RuntimeError("boom"))

    await _reaches_orders_and_not_billing("Pooled connection 1 reported an error", orders, billing)


async def test_CONTROL_a_standalone_pool_writes_a_line_no_service_streams():
    orders, billing = _two_services_streaming()

    await _pool(None).close()
    logger.complete()
    await asyncio.sleep(0.2)
    logger.complete()

    assert not [m for m in orders.messages + billing.messages if "pool closed" in m]
