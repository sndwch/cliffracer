"""Tests for native idempotency key generation and JetStream deduplication."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, Field

from cliffracer import (
    CliffracerService,
    IdempotencyContext,
    IdempotencyKeyError,
    ServiceConfig,
    StreamSpec,
    idempotent,
)
from cliffracer.core.idempotency import compute_payload_hash, format_nats_msg_id

pytestmark = pytest.mark.unit


class OrderRequest(BaseModel):
    order_id: str
    amount: float


class NestedOrder(BaseModel):
    data: OrderRequest


class TestIdempotencyContext:
    def test_context_set_get_reset_clear(self):
        """IdempotencyContext tracks per-task key and cleans up via token or clear."""
        assert IdempotencyContext.get() is None

        token = IdempotencyContext.set("k1")
        assert IdempotencyContext.get() == "k1"

        IdempotencyContext.reset(token)
        assert IdempotencyContext.get() is None

        IdempotencyContext.set("k2")
        assert IdempotencyContext.get() == "k2"
        IdempotencyContext.clear()
        assert IdempotencyContext.get() is None


class TestComputePayloadHash:
    def test_dynamic_envelope_fields_excluded(self):
        """Timestamp, correlation_id, and source_service do not alter payload hash."""
        p1 = {
            "order_id": "ord-1",
            "amount": 99.5,
            "timestamp": datetime.now(UTC).isoformat(),
            "correlation_id": "c1",
            "source_service": "srv1",
        }
        p2 = {
            "order_id": "ord-1",
            "amount": 99.5,
            "timestamp": "2020-01-01T00:00:00Z",
            "correlation_id": "c2",
            "source_service": "srv2",
        }
        assert compute_payload_hash(p1) == compute_payload_hash(p2)

    def test_dict_key_ordering_deterministic(self):
        """Key ordering in dictionaries produces identical hash."""
        d1 = {"a": 1, "b": 2, "c": {"x": 10, "y": 20}}
        d2 = {"c": {"y": 20, "x": 10}, "b": 2, "a": 1}
        assert compute_payload_hash(d1) == compute_payload_hash(d2)

    def test_pydantic_model_hashing(self):
        """Pydantic models are hashed identically to equivalent dictionaries."""
        model = OrderRequest(order_id="123", amount=45.0)
        d = {"order_id": "123", "amount": 45.0}
        assert compute_payload_hash(model) == compute_payload_hash(d)

    def test_base_model_with_different_default_timestamps_generates_identical_hash(self):
        """Two BaseModel instances with different default timestamps generate identical SHA-256 digests."""

        class TimestampedOrder(BaseModel):
            order_id: str
            timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
            correlation_id: str | None = None
            source_service: str | None = None

        m1 = TimestampedOrder(
            order_id="ord-999",
            timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
            correlation_id="corr-1",
            source_service="srv-1",
        )
        m2 = TimestampedOrder(
            order_id="ord-999",
            timestamp=datetime(2026, 9, 10, 18, 0, 0, tzinfo=UTC),
            correlation_id="corr-2",
            source_service="srv-2",
        )

        assert m1.timestamp != m2.timestamp
        h1 = compute_payload_hash(m1)
        h2 = compute_payload_hash(m2)
        assert h1 == h2
        assert len(h1) == 64


class TestFormatNatsMsgId:
    def test_subject_scoping(self):
        """Keys are scoped with subject prefix: f'{subject}:{key}'."""
        scoped = format_nats_msg_id("orders.created", "ord_101")
        assert scoped == "orders.created:ord_101"

    def test_already_scoped_key_preserved(self):
        """A key already beginning with subject prefix is not double-scoped."""
        scoped = format_nats_msg_id("orders.created", "orders.created:ord_101")
        assert scoped == "orders.created:ord_101"

    def test_oversized_key_hashed(self):
        """Keys or combined lengths exceeding 128 bytes are hashed to SHA-256."""
        long_key = "a" * 200
        scoped = format_nats_msg_id("orders.created", long_key)
        assert len(scoped) <= 128
        assert not scoped.endswith(long_key)

    def test_hash_payload_format(self):
        """When hash_payload=True, the formatted key is bounded and subject-scoped."""
        payload_hash = compute_payload_hash({"x": 1})
        scoped = format_nats_msg_id("orders.created", payload_hash, hash_payload=True)
        assert len(scoped) <= 128
        assert scoped.startswith("orders.created:")


class TestIdempotentDecorator:
    @pytest.mark.asyncio
    async def test_decorator_extracts_param_name(self):
        """@idempotent(key='order_id') extracts value and binds to IdempotencyContext."""
        captured_key = None

        @idempotent(key="order_id")
        async def process(order_id: str, amount: float):
            nonlocal captured_key
            captured_key = IdempotencyContext.get()
            return f"processed {order_id}"

        res = await process("ord_555", 10.0)
        assert res == "processed ord_555"
        assert captured_key == "ord_555"
        assert IdempotencyContext.get() is None

    @pytest.mark.asyncio
    async def test_decorator_extracts_dotted_path(self):
        """@idempotent(key='req.order_id') navigates nested objects."""
        captured_key = None

        @idempotent(key="req.order_id")
        async def handle(req: OrderRequest):
            nonlocal captured_key
            captured_key = IdempotencyContext.get()

        req = OrderRequest(order_id="nested_99", amount=5.0)
        await handle(req)
        assert captured_key == "nested_99"

    @pytest.mark.asyncio
    async def test_decorator_extracts_nested_dict(self):
        """@idempotent(key='order.id') navigates dictionaries."""
        captured_key = None

        @idempotent(key="order.id")
        async def handle(order: dict):
            nonlocal captured_key
            captured_key = IdempotencyContext.get()

        await handle({"id": "d_123"})
        assert captured_key == "d_123"

    @pytest.mark.asyncio
    async def test_decorator_callable_extractor(self):
        """@idempotent(key=callable) uses return value of callable."""
        captured_key = None

        @idempotent(key=lambda req: f"custom_{req.order_id}")
        async def handle(req: OrderRequest):
            nonlocal captured_key
            captured_key = IdempotencyContext.get()

        await handle(OrderRequest(order_id="c_1", amount=1.0))
        assert captured_key == "custom_c_1"

    @pytest.mark.asyncio
    async def test_decorator_hash_payload(self):
        """@idempotent(hash_payload=True) hashes domain arguments."""
        captured_key = None

        @idempotent(hash_payload=True)
        async def handle(order_id: str, amount: float):
            nonlocal captured_key
            captured_key = IdempotencyContext.get()

        await handle("ord_h", 50.0)
        expected = compute_payload_hash({"order_id": "ord_h", "amount": 50.0})
        assert captured_key == expected

    @pytest.mark.asyncio
    async def test_bare_decorator_defaults_to_hash_payload(self):
        """Bare @idempotent defaults to hash_payload=True."""
        captured_key = None

        @idempotent
        async def handle(item: str):
            nonlocal captured_key
            captured_key = IdempotencyContext.get()

        await handle("widget")
        expected = compute_payload_hash({"item": "widget"})
        assert captured_key == expected

    @pytest.mark.asyncio
    async def test_decorator_missing_key_raises(self):
        """Missing key raises IdempotencyKeyError."""

        @idempotent(key="missing_field")
        async def handle(order_id: str):
            pass

        with pytest.raises(IdempotencyKeyError, match="missing_field"):
            await handle("x")

    @pytest.mark.asyncio
    async def test_decorator_resets_context_on_exception(self):
        """Context is reset even when handler raises an exception."""

        @idempotent(key="order_id")
        async def faulty(order_id: str):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await faulty("err_1")

        assert IdempotencyContext.get() is None

    def test_sync_decorator_support(self):
        """Sync functions work identically with @idempotent."""
        captured = None

        @idempotent(key="x")
        def sync_fn(x: int):
            nonlocal captured
            captured = IdempotencyContext.get()
            return x * 2

        assert sync_fn(42) == 84
        assert captured == "42"
        assert IdempotencyContext.get() is None


class TestStreamSpecDuplicateWindow:
    def test_duplicate_window_default(self):
        """StreamSpec duplicate_window_seconds defaults to 120.0."""
        spec = StreamSpec(name="TEST", subjects=["test.*"])
        assert spec.duplicate_window_seconds == 120.0
        cfg = spec.to_stream_config()
        assert cfg.duplicate_window == 120.0

    def test_duplicate_window_custom(self):
        """Custom duplicate_window_seconds is propagated to StreamConfig."""
        spec = StreamSpec(name="TEST", subjects=["test.*"], duplicate_window_seconds=300.0)
        assert spec.duplicate_window_seconds == 300.0
        cfg = spec.to_stream_config()
        assert cfg.duplicate_window == 300.0

    def test_matches_checks_duplicate_window(self):
        """StreamSpec.matches() compares duplicate_window."""
        spec = StreamSpec(name="TEST", subjects=["test.*"], duplicate_window_seconds=300.0)
        matching_cfg = spec.to_stream_config()
        assert spec.matches(matching_cfg)

        drifted_cfg = StreamSpec(
            name="TEST", subjects=["test.*"], duplicate_window_seconds=120.0
        ).to_stream_config()
        assert not spec.matches(drifted_cfg)


class TestPublishEventIdempotency:
    @pytest.mark.asyncio
    async def test_explicit_idempotency_key_populates_header(self):
        """Passing idempotency_key to publish_event injects exact Nats-Msg-Id header."""
        published_headers = {}

        class FakeService(CliffracerService):
            def __init__(self):
                super().__init__(ServiceConfig(name="test_svc", jetstream_enabled=False))
                self.nc = AsyncMock()

                async def fake_pub(subj, data, headers=None):
                    nonlocal published_headers
                    published_headers = headers or {}

                self.nc.publish = fake_pub

        svc = FakeService()
        await svc.publish_event("orders.created", idempotency_key="order_123", order_id="order_123")

        assert "Nats-Msg-Id" in published_headers
        assert published_headers["Nats-Msg-Id"] == "orders.created:order_123"

    @pytest.mark.asyncio
    async def test_ambient_idempotency_context_populates_header(self):
        """Ambient IdempotencyContext is picked up by publish_event."""
        published_headers = {}

        class FakeService(CliffracerService):
            def __init__(self):
                super().__init__(ServiceConfig(name="test_svc", jetstream_enabled=False))
                self.nc = AsyncMock()

                async def fake_pub(subj, data, headers=None):
                    nonlocal published_headers
                    published_headers = headers or {}

                self.nc.publish = fake_pub

            @idempotent(key="order_id")
            async def process(self, order_id: str):
                await self.publish_event("orders.created", order_id=order_id)

        svc = FakeService()
        await svc.process("ambient_456")

        assert "Nats-Msg-Id" in published_headers
        assert published_headers["Nats-Msg-Id"] == "orders.created:ambient_456"

    @pytest.mark.asyncio
    async def test_idempotent_publishing_config_hashes_payload(self):
        """ServiceConfig.idempotent_publishing=True hashes domain payload when no key given."""
        published_headers = {}

        class FakeService(CliffracerService):
            def __init__(self):
                super().__init__(
                    ServiceConfig(
                        name="test_svc",
                        jetstream_enabled=False,
                        idempotent_publishing=True,
                    )
                )
                self.nc = AsyncMock()

                async def fake_pub(subj, data, headers=None):
                    nonlocal published_headers
                    published_headers = headers or {}

                self.nc.publish = fake_pub

        svc = FakeService()
        await svc.publish_event("orders.created", order_id="auto_789", amount=12.0)

        assert "Nats-Msg-Id" in published_headers
        msg_id = published_headers["Nats-Msg-Id"]
        assert msg_id.startswith("orders.created:")
        payload_hash = compute_payload_hash({"order_id": "auto_789", "amount": 12.0})
        assert payload_hash in msg_id


@pytest.mark.asyncio
async def test_live_jetstream_idempotent_deduplication():
    """Verify live NATS JetStream deduplication via Nats-Msg-Id within duplicate window."""
    import nats

    stream_name = "IDEMP_LIVE_STREAM"
    dlq_stream_name = "IDEMP_LIVE_DLQ"
    subject = "idemp.live.event"

    try:
        nc = await nats.connect("nats://127.0.0.1:4222", connect_timeout=2.0)
    except Exception:
        pytest.skip("Local NATS broker not available on nats://127.0.0.1:4222")

    js = nc.jetstream()

    # Clean up any leftover stream
    for s in (stream_name, dlq_stream_name):
        try:
            await js.delete_stream(s)
        except Exception:
            pass

    # Declare stream with 60s duplicate window and DLQ stream
    spec = StreamSpec(name=stream_name, subjects=[subject], duplicate_window_seconds=60.0)
    dlq_spec = StreamSpec(name=dlq_stream_name, subjects=["dlq.*"])
    await js.add_stream(spec.to_stream_config())
    await js.add_stream(dlq_spec.to_stream_config())

    class IdempService(CliffracerService):
        def __init__(self):
            super().__init__(
                ServiceConfig(
                    name="idemp_svc",
                    nats_url="nats://127.0.0.1:4222",
                    jetstream_enabled=True,
                    jetstream_streams=[spec, dlq_spec],
                )
            )

        @idempotent(key="order_id")
        async def emit_order(self, order_id: str, amount: float):
            return await self.publish_event(
                subject,
                order_id=order_id,
                amount=amount,
            )

    svc = IdempService()
    await svc.start()

    try:
        # Publish 1
        ack1 = await svc.emit_order("order_xyz", 100.0)
        assert ack1 is not None
        assert not getattr(ack1, "duplicate", False)
        assert ack1.seq == 1

        # Publish 2 with SAME idempotency key but different timestamp/correlation_id!
        ack2 = await svc.emit_order("order_xyz", 100.0)
        assert ack2 is not None
        # JetStream must detect the duplicate!
        assert ack2.duplicate is True
        assert ack2.seq == 1  # Sequence matches original message sequence

        # Verify stream message count is still 1
        info = await js.stream_info(stream_name)
        assert info.state.messages == 1

        # Publish 3 with DIFFERENT idempotency key
        ack3 = await svc.emit_order("order_abc", 200.0)
        assert ack3 is not None
        assert not getattr(ack3, "duplicate", False)
        assert ack3.seq == 2

    finally:
        await svc.stop()
        for s in (stream_name, dlq_stream_name):
            try:
                await js.delete_stream(s)
            except Exception:
                pass
        await nc.close()
