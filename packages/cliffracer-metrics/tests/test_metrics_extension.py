"""Tests for MetricsExtension and supporting metrics/pool components."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from cliffracer_metrics import (
    BatchProcessor,
    MetricsExtension,
    OptimizedNATSConnection,
    PerformanceMetrics,
)

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext


class Svc(CliffracerService):
    metrics = MetricsExtension()


def _ctx(kind: str, subject: str) -> WorkerContext:
    return WorkerContext(kind=kind, subject=subject, headers={}, correlation_id=None, payload={})


# Extension hook chain tests.


@pytest.mark.unit
async def test_a_dispatch_is_timed_and_counted():
    svc = Svc(ServiceConfig(name="m"))
    await svc.container._setup_extensions()

    async def ok():
        return 1

    await svc.container._run_worker(_ctx("rpc", "m.rpc.x"), ok)
    summary = svc.metrics.health_details()
    assert summary["rpc"]["count"] == 1
    assert summary["rpc"]["errors"] == 0
    assert summary["rpc"]["latency_ms"]["max"] >= 0


@pytest.mark.unit
async def test_an_error_is_counted_and_reraised():
    svc = Svc(ServiceConfig(name="m"))
    await svc.container._setup_extensions()

    async def bad():
        raise ValueError("x")

    with pytest.raises(ValueError):
        await svc.container._run_worker(_ctx("event", "e"), bad)
    assert svc.metrics.health_details()["event"]["errors"] == 1
    assert svc.metrics.health_details()["event"]["rejected"] == 0


class _Refuser(Extension):
    async def worker_setup(self, ctx):
        raise RejectMessage("not authorised")


@pytest.mark.unit
async def test_a_refusal_is_counted_as_rejected_and_not_as_an_error():
    """Verify RejectMessage is recorded under rejected rather than errors."""

    class Refusing(CliffracerService):
        metrics = MetricsExtension()
        refuser = _Refuser()

    svc = Refusing(ServiceConfig(name="r"))
    await svc.container._setup_extensions()

    async def handler():
        return 1

    with pytest.raises(RejectMessage):
        await svc.container._run_worker(_ctx("event", "e"), handler)

    stats = svc.metrics.health_details()["event"]
    assert stats["rejected"] == 1, stats
    assert stats["errors"] == 0, "a refusal must not read as a failure"
    assert stats["count"] == 1, "it was still a dispatch"


@pytest.mark.unit
async def test_two_services_do_not_share_metrics():
    """Verify metrics state is isolated per service instance."""
    a, b = Svc(ServiceConfig(name="a")), Svc(ServiceConfig(name="b"))
    await a.container._setup_extensions()
    await b.container._setup_extensions()

    assert a.metrics._count is not b.metrics._count
    assert a.metrics._errors is not b.metrics._errors
    assert a.metrics._latency is not b.metrics._latency

    async def ok():
        return 1

    # Not only identity: drive a real dispatch through one and require the
    # other to have seen nothing. Identity alone would also hold if both were
    # left as None, which is the vacuous pass this closes.
    await a.container._run_worker(_ctx("rpc", "a.rpc.x"), ok)
    assert a.metrics.health_details()["rpc"]["count"] == 1
    assert b.metrics.health_details() == {}


@pytest.mark.unit
async def test_no_contribution_before_setup():
    """/health must not report "not set up" as an error or a zero."""
    assert Svc(ServiceConfig(name="m")).metrics.health_details() is None


@pytest.mark.unit
async def test_the_latency_window_is_bounded():
    """A long-running service dispatches without limit and /health reads this
    list on every request, so it cannot grow forever."""
    from cliffracer_metrics.extension import _LATENCY_WINDOW

    svc = Svc(ServiceConfig(name="m"))
    await svc.container._setup_extensions()

    async def ok():
        return 1

    for _ in range(_LATENCY_WINDOW + 25):
        await svc.container._run_worker(_ctx("rpc", "m.rpc.x"), ok)

    assert len(svc.metrics._latency["rpc"]) == _LATENCY_WINDOW
    assert svc.metrics.health_details()["rpc"]["count"] == _LATENCY_WINDOW + 25


# ---- Library component tests ----------------------------------------------


@pytest.mark.unit
def test_performance_metrics_records_and_summarises():
    m = PerformanceMetrics()
    m.record_latency(12.5)
    m.record_latency(7.5)

    stats = m.get_latency_stats()
    assert stats["count"] == 2
    assert stats["min_ms"] == 7.5 and stats["max_ms"] == 12.5
    assert stats["mean_ms"] == 10.0

    summary = m.get_performance_summary()
    assert set(summary) >= {"latency", "throughput", "resources", "custom"}


@pytest.mark.unit
async def test_batch_processor_batches_by_size():
    """A full batch is handed to the processor as one call, not item by item."""
    seen = []

    async def processor(items):
        seen.append(list(items))
        return [None] * len(items)

    bp = BatchProcessor(batch_size=2, batch_timeout_ms=60_000)
    try:
        await asyncio.gather(
            bp.add_item("k", "a", processor),
            bp.add_item("k", "b", processor),
        )
    finally:
        await bp.shutdown()

    assert seen == [["a", "b"]], f"expected one batch of two, got {seen}"


# ---- the pool's credentials, moved with the pool --------------------------


class TestPoolUsesCredentials:
    @pytest.mark.asyncio
    async def test_pool_forwards_credentials_to_every_connection(self):
        pool = OptimizedNATSConnection(
            nats_url="nats://h:4222",
            max_connections=3,
            auth_kwargs={"user": "u", "password": "p"},
        )
        with patch("nats.connect", new=AsyncMock()) as m:
            await pool.connect()
        assert m.call_count == 3, "every pooled connection must authenticate"
        for call in m.call_args_list:
            assert call.kwargs["user"] == "u"
            assert call.kwargs["password"] == "p"

    @pytest.mark.asyncio
    async def test_pool_without_credentials_passes_no_auth_kwargs(self):
        """Backward compatibility: an unauthenticated pool must produce the
        same call it did before auth existed."""
        pool = OptimizedNATSConnection(nats_url="nats://h:4222", max_connections=1)
        with patch("nats.connect", new=AsyncMock()) as m:
            await pool.connect()
        kwargs = m.call_args.kwargs
        for key in ("user", "password", "token", "user_credentials"):
            assert key not in kwargs

    @pytest.mark.asyncio
    async def test_pool_and_service_agree_on_credentials(self):
        """Verify pool uses identical authentication credentials as the service."""
        cfg = ServiceConfig(
            name="s", nats_user="u", nats_password="p", nats_credentials_file="/c.creds"
        )
        pool = OptimizedNATSConnection(
            nats_url=cfg.nats_url, max_connections=1, auth_kwargs=cfg.nats_auth_kwargs()
        )
        with patch("nats.connect", new=AsyncMock()) as m:
            await pool.connect()
        sent = {k: v for k, v in m.call_args.kwargs.items() if k in cfg.nats_auth_kwargs()}
        assert sent == cfg.nats_auth_kwargs()


# ---- Health port binding tests --------------------------------------------


@pytest.mark.unit
@pytest.mark.nats_required
async def test_starting_a_service_does_not_bind_the_default_port():
    """Verify service startup allocates an ephemeral health port under test fixtures."""
    svc = Svc(ServiceConfig(name="port-probe"))
    assert svc.config.health_port == 8000, "the default this guard exists for"
    await svc.start()
    try:
        bound = svc.health_listener.port
        assert bound not in (0, 8000), f"bound the default port: {bound}"
    finally:
        await svc.stop()


@pytest.mark.unit
def test_pool_is_connected_reflects_actual_connection_status():
    """Verify pool.is_connected checks conn.is_connected rather than conn.is_closed."""
    from unittest.mock import MagicMock

    from cliffracer_metrics.connection_pool import OptimizedNATSConnection

    pool = OptimizedNATSConnection()
    mock_conn = MagicMock()
    mock_conn.is_connected = False
    mock_conn.is_closed = False
    pool._connections = [mock_conn]

    assert pool.is_connected is False
    assert pool.get_stats()["active_connections"] == 0

    mock_conn.is_connected = True
    assert pool.is_connected is True
    assert pool.get_stats()["active_connections"] == 1
