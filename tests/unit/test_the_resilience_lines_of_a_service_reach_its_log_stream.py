"""A refused call's warning is bound to the service that refused it, so its log stream carries it.

A NATS log sink publishes only the records bound to its service. `ResilienceExtension` wrote the
"rate limit exceeded" warning through the bare loguru logger, so the line an operator reads to
learn a service is shedding load reached no `logs.<service>.<level>` stream. It is now written
through the logger the extension base binds to its service. The shared limiter's own lines have no
service to name, so they stay unbound.
"""

import asyncio
import json

import pytest
from cliffracer_logging import LoggingConfig
from cliffracer_resilience import ResilienceExtension, rate_limit
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing import wait_until
from cliffracer.testing.messages import MockMessage

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


class Limited(CliffracerService):
    resilience = ResilienceExtension()

    @rpc
    @rate_limit(calls=1, window=60.0)
    async def go(self) -> int:
        return 1


async def _call(svc) -> dict:
    message = MockMessage(
        "orders.rpc.go", data=b"{}", headers={"Content-Type": "application/json"}, reply="_INBOX.r"
    )
    await svc.container._handle_rpc_request(message)
    return json.loads(message.responded_data)


async def test_the_rate_limit_warning_is_in_its_services_stream_and_in_no_other():
    orders, billing = _two_services_streaming()
    svc = Limited(ServiceConfig(name="orders", subject_prefix=None, health_port=0))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    assert (await _call(svc))["success"] is True
    assert (await _call(svc))["success"] is False

    await _reaches_orders_and_not_billing("rate limit exceeded for key", orders, billing)


class Partitioned(CliffracerService):
    resilience = ResilienceExtension()

    @rpc
    @rate_limit(calls=5, window=60.0, key="x-client")
    async def go(self) -> int:
        return 1


async def test_the_refusal_of_a_message_with_no_rate_limit_key_is_in_its_services_stream():
    orders, billing = _two_services_streaming()
    svc = Partitioned(ServiceConfig(name="orders", subject_prefix=None, health_port=0))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    assert (await _call(svc))["success"] is False

    await _reaches_orders_and_not_billing(
        "refused a message with no rate-limit key", orders, billing
    )
