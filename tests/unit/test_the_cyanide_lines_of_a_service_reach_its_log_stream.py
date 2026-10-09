"""A cyanide extension's lines are bound to the service it is declared on, so its stream carries them.

A NATS log sink publishes only the records bound to its service. The extension wrote through the
bare loguru logger, so the seed it logs in setup (the one line that says how to replay a run) and
its warnings reached no `logs.<service>.<level>` stream. It now writes through the logger the
extension base binds to its service. The seed line must be in `orders`' sink and not in `billing`'s.

The sink here is attached by hand before `setup`. In a running service `LoggingExtension` attaches its
sink after the connection is made, which is after `setup`, so the seed line reaches a stream there only
through a sink the host attached earlier.
"""

import asyncio

import pytest
from cliffracer_cyanide import CyanideConfig, CyanideExtension
from cliffracer_logging import LoggingConfig
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
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


async def test_the_seed_line_is_in_its_services_stream_and_in_no_other():
    orders, billing = _two_services_streaming()

    class Svc(CliffracerService):
        cyanide = CyanideExtension(CyanideConfig(enabled=True, seed="the-seed"))

    svc = Svc(ServiceConfig(name="orders", subject_prefix=None, health_port=0))
    await svc.container._setup_extensions()

    await _reaches_orders_and_not_billing("cyanide seeded with 'the-seed'", orders, billing)


async def test_a_mode_requested_while_disabled_is_in_its_services_stream():
    from cliffracer.core.extension import WorkerContext

    orders, billing = _two_services_streaming()

    class Svc(CliffracerService):
        cyanide = CyanideExtension(CyanideConfig(enabled=False))

    svc = Svc(ServiceConfig(name="orders", subject_prefix=None, health_port=0))
    await svc.container._setup_extensions()
    ctx = WorkerContext(
        kind="rpc",
        subject="orders.rpc.go",
        headers={"x-cyanide-mode": "slow"},
        correlation_id="c",
        payload={},
    )
    await svc.cyanide.worker_setup(ctx)

    await _reaches_orders_and_not_billing("requested while disabled", orders, billing)


async def test_a_header_that_is_not_a_number_of_seconds_is_in_its_services_stream():
    orders, billing = _two_services_streaming()

    class Svc(CliffracerService):
        cyanide = CyanideExtension(CyanideConfig(enabled=True))

    svc = Svc(ServiceConfig(name="orders", subject_prefix=None, health_port=0))
    await svc.container._setup_extensions()

    assert svc.cyanide._header_seconds({"x-cyanide-delay": "soon"}, "x-cyanide-delay") is None

    await _reaches_orders_and_not_billing("ignoring header x-cyanide-delay", orders, billing)
