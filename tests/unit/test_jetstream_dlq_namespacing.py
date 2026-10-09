"""Regression tests for DLQ subject namespacing decoupling."""

from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec

pytestmark = pytest.mark.unit


def test_namespaced_service_dlq_covered_by_root_stream():
    """A namespaced service's default DLQ subject has no namespace in it, so root 'dlq.*' covers it."""
    svc = CliffracerService(
        ServiceConfig(
            name="orders",
            namespace="prod",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
        )
    )
    # The namespace is set and does not reach the subject: that is what the claim relies on.
    assert svc.config.namespace == "prod"
    assert svc.container._format_dlq_subject() == "dlq.orders"
    # Must not raise StreamDeclarationError
    svc.container._assert_dlq_covered()


def test_namespaced_service_dlq_covered_by_gt_stream():
    """The same subject is covered by a root 'dlq.>' JetStream stream."""
    svc = CliffracerService(
        ServiceConfig(
            name="orders",
            namespace="prod",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.>"])],
        )
    )
    assert svc.container._format_dlq_subject() == "dlq.orders"
    svc.container._assert_dlq_covered()


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
    with pytest.raises(StreamDeclarationError):
        svc_bad_stream.container._assert_dlq_covered()


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


class _SendWatcher(Extension):
    """Records every outbound message that passes through the send hooks."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def before_call(self, ctx) -> None:
        self.sent.append(ctx.kind)


async def _watched_service() -> tuple[CliffracerService, _SendWatcher]:
    class Watched(CliffracerService):
        watcher = _SendWatcher()

    svc = Watched(ServiceConfig(name="orders", namespace="prod"))
    svc.nc = AsyncMock()
    await svc.container._setup_extensions()
    return svc, svc.watcher


@pytest.mark.asyncio
async def test_the_send_watcher_sees_an_ordinary_publish():
    """The control for the test below: this extension is on the send path, so
    its silence there is a finding and not an artefact of how it is declared."""
    svc, watcher = await _watched_service()

    await svc.publish_event("orders.created", order_id="o1")

    assert watcher.sent == ["publish_event"]


@pytest.mark.asyncio
async def test_publish_dlq_bypasses_send_hooks():
    """_publish_dlq does not run application extensions' send hooks.

    A dead-letter publish that went through them could be altered or refused by
    a tracing or retry extension, or recurse when an extension's own failure is
    dead-lettered.
    """
    svc, watcher = await _watched_service()

    await svc.container._publish_dlq(
        "dlq.orders",
        payload=b'{"test": 1}',
        headers={},
    )

    assert watcher.sent == []
    svc.nc.publish.assert_awaited_once()
    assert svc.nc.publish.await_args.args[0] == "dlq.orders"
