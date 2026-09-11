"""Regression tests for DLQ subject namespacing decoupling."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec


@pytest.mark.unit
def test_namespaced_service_dlq_covered_by_root_stream():
    """Namespaced service DLQ is covered by root 'dlq.*' JetStream stream."""
    svc = CliffracerService(
        ServiceConfig(
            name="orders",
            namespace="prod",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
        )
    )
    svc.container.js = MagicMock()
    # Must not raise StreamDeclarationError
    svc.container._assert_dlq_covered()


@pytest.mark.unit
def test_namespaced_service_dlq_covered_by_gt_stream():
    """Namespaced service DLQ is covered by root 'dlq.>' JetStream stream."""
    svc = CliffracerService(
        ServiceConfig(
            name="orders",
            namespace="prod",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.>"])],
        )
    )
    svc.container.js = MagicMock()
    svc.container._assert_dlq_covered()


@pytest.mark.unit
def test_custom_dlq_template_with_namespace():
    """Custom dlq_subject formatting supports both {namespace} and {service}."""
    svc = CliffracerService(
        ServiceConfig(
            name="orders",
            namespace="prod",
            dlq_subject="{namespace}.dlq.{service}",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="DLQ", subjects=["prod.dlq.*"])],
        )
    )
    svc.container.js = MagicMock()
    assert svc.container._format_dlq_subject() == "prod.dlq.orders"
    svc.container._assert_dlq_covered()

    # Fails if stream only claims dlq.*
    svc_bad_stream = CliffracerService(
        ServiceConfig(
            name="orders",
            namespace="prod",
            dlq_subject="{namespace}.dlq.{service}",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
        )
    )
    svc_bad_stream.container.js = MagicMock()
    with pytest.raises(StreamDeclarationError):
        svc_bad_stream.container._assert_dlq_covered()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_dlq_publishes_verbatim_root_subject():
    """_publish_dlq emits directly to raw subject without namespace prefixing."""
    svc = CliffracerService(ServiceConfig(name="orders", namespace="prod"))
    svc.container.nc = AsyncMock()

    await svc.container._publish_dlq(
        "dlq.orders",
        payload={"order_id": "123"},
        error="validation failure",
    )

    svc.container.nc.publish.assert_awaited_once()
    call_args = svc.container.nc.publish.call_args
    assert call_args.args[0] == "dlq.orders"
    assert "prod.dlq.orders" != call_args.args[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_dlq_bypasses_send_hooks():
    """_publish_dlq does not invoke application extension send hooks."""
    hook_called = False

    async def mock_hook(ctx, call):
        nonlocal hook_called
        hook_called = True
        return await call()

    svc = CliffracerService(ServiceConfig(name="orders", namespace="prod"))
    svc.container.nc = AsyncMock()
    svc.container._send_hooks = [mock_hook]

    await svc.container._publish_dlq(
        "dlq.orders",
        payload=b'{"test": 1}',
        headers={},
    )

    assert hook_called is False
    svc.container.nc.publish.assert_awaited_once()
