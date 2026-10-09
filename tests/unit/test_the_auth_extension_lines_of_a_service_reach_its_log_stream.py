"""A failing outbound token factory is logged under the service that sent the call.

A NATS log sink publishes only the records bound to its service. `AuthExtension` wrote the error
for a factory that raised through the bare loguru logger, so the line saying a call went out
without its token reached no `logs.<service>.<level>` stream. It is now written through the logger
the extension base binds to its service.
"""

import asyncio

import pytest
from cliffracer_auth import AuthConfig, AuthExtension, SimpleAuthService
from cliffracer_logging import LoggingConfig
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import WorkerContext
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


def _factory_that_raises() -> str:
    raise RuntimeError("no token today")


async def test_the_factory_failure_is_in_its_services_stream_and_in_no_other():
    orders, billing = _two_services_streaming()
    issuer = SimpleAuthService(AuthConfig(secret_key="the-signing-key-" + "k" * 24))

    class Svc(CliffracerService):
        auth = AuthExtension(issuer, outbound_token_factory=_factory_that_raises)

    svc = Svc(ServiceConfig(name="orders", subject_prefix=None, health_port=0))
    await svc.container._setup_extensions()
    ctx = WorkerContext(
        kind="call_rpc",
        subject="inventory.rpc.check",
        headers={},
        correlation_id="c",
        payload={},
    )

    await svc.auth.before_call(ctx)

    await _reaches_orders_and_not_billing("outbound_token_factory raised", orders, billing)
