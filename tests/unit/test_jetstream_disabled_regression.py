"""Tests ensuring service behavior when jetstream_enabled=False."""

from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


class _Svc(CliffracerService):
    @listener("orders.created", fanout=True)
    async def on_created(self, subject: str) -> None:
        pass

    @listener("orders.shipped", durable="orders-shipped", fanout=True)
    async def on_shipped(self, subject: str) -> None:
        pass


def _service(**overrides):
    """Build a service with handlers discovered before transport mocks attach.

    Discovery only reads decorator attributes off the class's bound methods.
    Running it before ``nc``/``js`` are replaced with mocks means it never
    scans the mocks themselves — an ``AsyncMock`` answers True to any
    ``hasattr()``, so attaching it first would get it misregistered as a
    decorated handler.
    """
    cfg = ServiceConfig(name="order_svc", **overrides)
    svc = _Svc(cfg)
    svc._discover_handlers()
    svc.nc = AsyncMock()
    svc.js = None
    return svc


@pytest.mark.asyncio
async def test_js_is_none_when_disabled():
    svc = _service()
    assert svc.config.jetstream_enabled is False
    assert svc.js is None


@pytest.mark.asyncio
async def test_publish_event_is_a_single_core_publish():
    svc = _service()
    await svc.publish_event("orders.created", order_id="o1")

    assert svc.nc.publish.await_count == 1
    call = svc.nc.publish.call_args
    assert call.args[0] == "orders.created"
    payload = call.args[1]
    assert b'"order_id": "o1"' in payload
    assert "correlation_id" in call.kwargs["headers"]


@pytest.mark.asyncio
async def test_publish_event_still_namespaces_when_disabled():
    svc = _service(namespace="app1")
    await svc.publish_event("orders.created", order_id="o1")
    assert svc.nc.publish.call_args.args[0] == "app1.orders.created"


@pytest.mark.asyncio
async def test_durable_listener_still_uses_a_core_subscription():
    """durable= is inert without the flag. This is the argument's whole contract."""
    svc = _service()
    await svc.container.setup_subscriptions()

    subscribed = [c.args[0] for c in svc.nc.subscribe.call_args_list]
    assert "orders.created" in subscribed
    assert "orders.shipped" in subscribed


@pytest.mark.asyncio
async def test_declared_streams_are_not_provisioned_when_disabled():
    """jetstream_streams set but the flag off must touch the server not at all.

    Provisioning happens in start(), not _setup_subscriptions() — this has to
    drive start() itself, with a real-looking self.js in place beforehand, or
    the three provisioning assertions below are unreachable no matter what
    the flag does.
    """
    svc = _service(jetstream_streams=[StreamSpec(name="X", subjects=["orders.>"])])

    mock_nc = AsyncMock()
    mock_nc.is_closed = False
    svc.js = AsyncMock()  # if anything provisions or subscribes via JetStream, it shows up here

    with patch("nats.connect", return_value=mock_nc):
        await svc.start()

    assert svc.js.add_stream.await_count == 0
    assert svc.js.update_stream.await_count == 0
    assert svc.js.streams_info.await_count == 0
    assert svc.js.subscribe.await_count == 0

    # Cancel subscription handler tasks.
    await svc.stop()


@pytest.mark.asyncio
async def test_a_raising_handler_is_caught_and_the_next_handler_still_runs():
    """The core path logs and continues. It must not start propagating."""
    calls = []

    class _Multi(CliffracerService):
        @listener("orders.*", fanout=True)
        async def a_raising_wildcard_handler(self, subject: str, order_id: str = ""):
            calls.append("raising")
            raise RuntimeError("boom")

        @listener("orders.created", fanout=True)
        async def z_surviving_exact_handler(self, subject: str, order_id: str = ""):
            calls.append("surviving")

    svc = _Multi(ServiceConfig(name="order_svc"))
    svc._discover_handlers()
    svc.nc = AsyncMock()

    msg = AsyncMock()
    msg.subject = "orders.created"
    msg.data = b'{"order_id": "o1"}'
    msg.headers = None

    await svc.container._handle_event(msg)  # must not raise

    assert calls == ["raising", "surviving"]
