"""Unit tests for correlation ID header standardization.

Verifies canonical X-Correlation-ID header extraction, case-insensitivity,
candidate fallback precedence, dual-emission in client and container,
and CorrelationExtension behavior.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from cliffracer import ServiceConfig
from cliffracer.client import ServiceClient
from cliffracer.core.container import Container
from cliffracer.core.correlation import (
    CorrelationContext,
    correlation_id_var,
)
from cliffracer.core.correlation_extension import CorrelationExtension
from cliffracer.core.extension import WorkerContext

pytestmark = pytest.mark.unit


def test_extract_from_headers_case_insensitivity() -> None:
    """extract_from_headers matches canonical header regardless of casing."""
    cases = [
        {"X-Correlation-ID": "corr-123"},
        {"x-correlation-id": "corr-123"},
        {"X-CORRELATION-ID": "corr-123"},
        {"X-cOrReLaTiOn-Id": "corr-123"},
    ]
    for headers in cases:
        assert CorrelationContext.extract_from_headers(headers) == "corr-123"


def test_extract_from_headers_candidate_priority() -> None:
    """Precedence: x-correlation-id > x-request-id > x-trace-id > correlation-id > correlation_id."""
    headers = {
        "x-correlation-id": "cid-high",
        "x-request-id": "rid-mid",
        "x-trace-id": "tid-low",
        "correlation-id": "hyphen-low",
        "correlation_id": "underscore-lowest",
    }
    assert CorrelationContext.extract_from_headers(headers) == "cid-high"

    del headers["x-correlation-id"]
    assert CorrelationContext.extract_from_headers(headers) == "rid-mid"

    del headers["x-request-id"]
    assert CorrelationContext.extract_from_headers(headers) == "tid-low"

    del headers["x-trace-id"]
    assert CorrelationContext.extract_from_headers(headers) == "hyphen-low"

    del headers["correlation-id"]
    assert CorrelationContext.extract_from_headers(headers) == "underscore-lowest"


def test_extract_from_headers_legacy_underscore() -> None:
    """Legacy correlation_id header key is recognized and extracted."""
    headers = {"correlation_id": "legacy-corr-001"}
    assert CorrelationContext.extract_from_headers(headers) == "legacy-corr-001"


def test_extract_from_headers_edge_cases() -> None:
    """Gracefully handles None, non-dict, empty dict, and whitespace/empty values."""
    assert CorrelationContext.extract_from_headers(None) is None
    assert CorrelationContext.extract_from_headers({}) is None
    assert CorrelationContext.extract_from_headers([]) is None  # type: ignore[arg-type]

    # Empty string falls through to next candidate
    headers = {
        "X-Correlation-ID": "",
        "x-request-id": "   ",
        "correlation_id": "fallback-id",
    }
    assert CorrelationContext.extract_from_headers(headers) == "fallback-id"

    # Non-string value converted to string
    assert CorrelationContext.extract_from_headers({"X-Correlation-ID": 12345}) == "12345"


@pytest.mark.asyncio
async def test_correlation_extension_extracts_x_correlation_id() -> None:
    """CorrelationExtension.worker_setup extracts canonical X-Correlation-ID from ctx.headers."""
    ext = CorrelationExtension()
    ctx = WorkerContext(
        kind="rpc",
        subject="test.call",
        headers={"X-Correlation-ID": "ext-trace-1"},
        correlation_id=None,
        payload={"foo": "bar"},
    )

    await ext.worker_setup(ctx)
    try:
        assert ctx.correlation_id == "ext-trace-1"
        assert correlation_id_var.get() == "ext-trace-1"
    finally:
        await ext.worker_teardown(ctx)
    assert correlation_id_var.get() is None


@pytest.mark.asyncio
async def test_correlation_extension_header_precedence_over_payload() -> None:
    """Headers take precedence over payload.correlation_id per wire protocol §4.2."""
    ext = CorrelationExtension()
    ctx = WorkerContext(
        kind="event",
        subject="orders.created",
        headers={"X-Correlation-ID": "from-header"},
        correlation_id=None,
        payload={"correlation_id": "from-payload", "amount": 42},
    )

    await ext.worker_setup(ctx)
    try:
        assert ctx.correlation_id == "from-header"
    finally:
        await ext.worker_teardown(ctx)


@pytest.mark.asyncio
async def test_correlation_extension_payload_fallback() -> None:
    """If headers lack correlation ID, worker_setup falls back to payload.correlation_id."""
    ext = CorrelationExtension()
    ctx = WorkerContext(
        kind="event",
        subject="orders.created",
        headers={"Content-Type": "application/json"},
        correlation_id=None,
        payload={"correlation_id": "payload-id-123"},
    )

    await ext.worker_setup(ctx)
    try:
        assert ctx.correlation_id == "payload-id-123"
    finally:
        await ext.worker_teardown(ctx)


@pytest.mark.asyncio
async def test_correlation_extension_generates_new_id_when_missing() -> None:
    """Generates fresh corr_<hex16> when neither header nor payload contains one."""
    ext = CorrelationExtension()
    ctx = WorkerContext(
        kind="rpc",
        subject="test.call",
        headers={},
        correlation_id=None,
        payload={},
    )

    await ext.worker_setup(ctx)
    try:
        assert ctx.correlation_id is not None
        assert ctx.correlation_id.startswith("corr_")
        assert len(ctx.correlation_id) == 21  # corr_ + 16 hex chars
    finally:
        await ext.worker_teardown(ctx)


def test_client_headers_for_send_injects_canonical_and_legacy() -> None:
    """ServiceClient._headers_for_send injects X-Correlation-ID and dual-emits correlation_id."""
    client = ServiceClient(headers={"authorization": "bearer xyz"})
    headers = client._headers_for_send()

    assert "X-Correlation-ID" in headers
    assert "correlation_id" in headers
    assert headers["X-Correlation-ID"] == headers["correlation_id"]
    assert len(headers["X-Correlation-ID"]) > 0
    assert headers["authorization"] == "bearer xyz"


def test_container_send_context_injects_canonical_and_legacy() -> None:
    """Container._send_context injects X-Correlation-ID and dual-emits correlation_id."""
    cfg = ServiceConfig(name="test_svc", version="1.0.0")
    dummy_service = MagicMock()
    container = Container(dummy_service, cfg)

    ctx = container._send_context("rpc", "target.method", {"x": 1}, "test-cid-999")
    assert ctx.headers["X-Correlation-ID"] == "test-cid-999"
    assert ctx.headers["correlation_id"] == "test-cid-999"
    assert ctx.headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_container_publish_dlq_injects_correlation_headers() -> None:
    """Container._publish_dlq injects X-Correlation-ID and correlation_id into DLQ message headers."""
    cfg = ServiceConfig(name="test_svc", version="1.0.0", dlq_subject="dlq.{service}")
    dummy_service = MagicMock()
    container = Container(dummy_service, cfg)
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    container.nc = mock_nc

    CorrelationContext.set("ambient-trace-dlq")
    try:
        await container._publish_dlq("dlq.test_svc", payload={"err": "poison"})
        mock_nc.publish.assert_called_once()
        _, call_kwargs = mock_nc.publish.call_args
        published_headers = call_kwargs["headers"]
        assert published_headers["X-Correlation-ID"] == "ambient-trace-dlq"
        assert published_headers["correlation_id"] == "ambient-trace-dlq"
    finally:
        CorrelationContext.clear()
