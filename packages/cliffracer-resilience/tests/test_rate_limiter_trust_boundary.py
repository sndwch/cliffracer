"""Distributed tenant limits remain authoritative and secret-free."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nats.js.errors
import pytest
from cliffracer_resilience import (
    KvRateLimiter,
    RateLimitConfig,
    RateLimiterUnavailableError,
    RateLimitExceeded,
    RateLimitKeyError,
    ResilienceExtension,
)
from loguru import logger

from cliffracer.core.extension import WorkerContext

pytestmark = pytest.mark.unit


def request_context(*, headers=None, payload=None) -> WorkerContext:
    return WorkerContext(
        kind="rpc",
        subject="billing.rpc.charge",
        headers=headers or {},
        correlation_id="invoice-123",
        payload=payload or {},
        data={"handler_name": "charge"},
    )


async def test_an_authenticated_header_cannot_be_shadowed_by_the_request_body():
    config = RateLimitConfig(calls=1, window=60, key="tenant-id")
    context = request_context(
        headers={"Tenant-ID": "tenant-authenticated"},
        payload={"tenant-id": "tenant-selected-by-caller"},
    )

    assert config.resolve_key(context) == "tenant-authenticated"


async def test_payload_partitioning_requires_an_explicit_declaration():
    context = request_context(payload={"tenant_id": "tenant-a"})

    with pytest.raises(RateLimitKeyError, match="header source"):
        RateLimitConfig(calls=1, window=60, key="tenant_id").resolve_key(context)

    config = RateLimitConfig(
        calls=1,
        window=60,
        key="tenant_id",
        key_source="payload",
    )
    assert config.resolve_key(context) == "tenant-a"


async def test_callable_partition_errors_do_not_switch_to_another_authority():
    def tenant_from_context(context):
        if isinstance(context, dict):
            return context["tenant_id"]
        raise TypeError("broken tenant resolver")

    config = RateLimitConfig(calls=1, window=60, key=tenant_from_context)

    with pytest.raises(TypeError, match="broken tenant resolver"):
        config.resolve_key(request_context(payload={"tenant_id": "tenant-a"}))


@pytest.mark.parametrize(
    ("key", "key_source", "message"),
    [
        ("tenant_id", "headers", "must be header, payload, or context"),
        (None, "payload", "requires a rate-limit key"),
        ("tenant_id", "context", "header or payload"),
        (lambda context: context.subject, "header", "context or payload"),
    ],
)
async def test_partition_sources_must_match_the_key_kind(key, key_source, message):
    with pytest.raises(ValueError, match=message):
        RateLimitConfig(calls=1, window=60, key=key, key_source=key_source)


async def test_corrupt_distributed_state_refuses_work_instead_of_granting_a_local_budget():
    store = AsyncMock()
    store.get.return_value = SimpleNamespace(value=b'["poison"]', revision=7)
    limiter = KvRateLimiter(kv=store)

    with pytest.raises(RateLimiterUnavailableError) as refusal:
        await limiter.acquire("tenant-a", calls=2, window=60)

    assert type(refusal.value.__cause__).__name__ == "_RateLimitStateError"
    assert limiter.health_details()["status"] == "unavailable"
    store.update.assert_not_awaited()
    store.create.assert_not_awaited()


async def test_cas_exhaustion_does_not_multiply_the_global_budget(monkeypatch):
    store = AsyncMock()
    store.get.return_value = SimpleNamespace(value=b"[]", revision=1)
    store.update.side_effect = nats.js.errors.KeyWrongLastSequenceError()
    limiter = KvRateLimiter(kv=store, max_retries=3)
    sleep = AsyncMock()
    monkeypatch.setattr("cliffracer_resilience.rate_limiter.asyncio.sleep", sleep)

    with pytest.raises(RateLimiterUnavailableError):
        await limiter.acquire("tenant-a", calls=3, window=60)

    assert store.update.await_count == 3
    assert sleep.await_count == 3
    assert limiter.health_details()["fallback_total"] == 0


async def test_cas_retry_records_the_time_of_the_successful_attempt(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("cliffracer_resilience.rate_limiter.time.time", lambda: clock[0])

    class Store:
        def __init__(self) -> None:
            self.value = None
            self.conflict = True

        async def get(self, key):
            if self.value is None:
                raise nats.js.errors.KeyNotFoundError()
            return SimpleNamespace(value=self.value, revision=1)

        async def create(self, key, value):
            if self.conflict:
                self.conflict = False
                raise nats.js.errors.KeyWrongLastSequenceError()
            self.value = value

        async def update(self, key, value, last):
            self.value = value

    store = Store()
    limiter = KvRateLimiter(kv=store)

    async def retry(attempt):
        clock[0] += 0.01

    monkeypatch.setattr(limiter, "_retry_cas", retry)

    assert await limiter.acquire("tenant-a", calls=1, window=0.005)
    assert not await limiter.acquire("tenant-a", calls=1, window=0.005)


async def test_explicit_local_fallback_is_observable_and_logs_only_one_transition():
    secret = "Bearer sk-live-do-not-retain\nforged-log-line"
    store = AsyncMock()
    store.get.side_effect = ConnectionError(secret)
    limiter = KvRateLimiter(kv=store, in_memory_fallback=True)
    output = io.StringIO()
    sink = logger.add(output, format="{message}")
    try:
        assert await limiter.acquire(secret, calls=3, window=60)
        assert await limiter.acquire(secret, calls=3, window=60)
    finally:
        logger.remove(sink)

    details = limiter.health_details()
    assert details == {
        "backend": "nats-kv",
        "status": "degraded",
        "fallback_enabled": True,
        "fallback_total": 2,
        "last_error_type": "ConnectionError",
    }
    assert output.getvalue().count("distributed backend degraded") == 1
    assert secret not in output.getvalue()
    assert "forged-log-line" not in output.getvalue()


async def test_opening_failure_preserves_the_backend_error_in_degraded_health():
    js = AsyncMock()
    js.key_value.side_effect = ConnectionError("broker offline")
    limiter = KvRateLimiter(js=js, in_memory_fallback=True)

    assert await limiter.acquire("tenant-a", calls=1, window=60)

    assert limiter.health_details()["last_error_type"] == "ConnectionError"
    js.create_key_value.assert_not_awaited()


async def test_opening_failure_refuses_work_by_default():
    js = AsyncMock()
    js.key_value.side_effect = ConnectionError("broker offline")
    limiter = KvRateLimiter(js=js)

    with pytest.raises(RateLimiterUnavailableError):
        await limiter.acquire("tenant-a", calls=1, window=60)

    assert limiter.health_details()["status"] == "unavailable"
    js.create_key_value.assert_not_awaited()


async def test_partition_credentials_are_never_used_as_kv_key_names():
    secret = "Bearer eyJhbGciOi.SUPER-SECRET-TOKEN"
    store = AsyncMock()
    store.get.side_effect = nats.js.errors.KeyNotFoundError()
    store.create.return_value = 1
    limiter = KvRateLimiter(kv=store)

    assert await limiter.acquire(secret, calls=1, window=60)

    stored_key = store.create.await_args.args[0]
    assert stored_key.startswith("sha256_")
    assert len(stored_key) == len("sha256_") + 64
    assert secret not in stored_key
    assert json.loads(store.create.await_args.args[1])


async def test_capacity_logs_and_refusal_metadata_contain_only_a_key_fingerprint():
    secret = "Bearer sk-live-do-not-log"
    limiter = AsyncMock()
    limiter.acquire.return_value = False
    limiter.get_retry_after.return_value = 3.0
    extension = ResilienceExtension(limiter=limiter)
    extension._rate_limits["charge"] = RateLimitConfig(
        calls=1,
        window=60,
        key="authorization",
    )
    context = request_context(headers={"Authorization": secret})
    output = io.StringIO()
    sink = logger.add(output, format="{message}")
    try:
        with pytest.raises(RateLimitExceeded) as refusal:
            await extension.worker_setup(context)
    finally:
        logger.remove(sink)

    assert secret not in output.getvalue()
    assert "sha256:" in output.getvalue()
    assert secret not in str(getattr(refusal.value, "details", {}))
    assert refusal.value.details["key"].startswith("sha256:")


async def test_extension_health_reports_when_the_global_bound_is_degraded():
    limiter = KvRateLimiter(kv=AsyncMock(), in_memory_fallback=True)
    limiter._kv.get.side_effect = ConnectionError("offline")
    extension = ResilienceExtension(limiter=limiter)

    assert await limiter.acquire("tenant-a", calls=1, window=60)

    assert extension.health_details()["rate_limiter"]["status"] == "degraded"


async def test_extension_health_reports_a_degraded_handler_bound():
    limiter = KvRateLimiter(kv=AsyncMock(), in_memory_fallback=True)
    limiter._kv.get.side_effect = ConnectionError("offline")
    extension = ResilienceExtension()
    extension._rate_limits["charge"] = RateLimitConfig(
        calls=1,
        window=60,
        limiter=limiter,
    )
    context = request_context()

    try:
        await extension.worker_setup(context)
    finally:
        await extension.worker_teardown(context)

    assert extension.health_details()["handler_rate_limiters"]["charge"]["status"] == ("degraded")
