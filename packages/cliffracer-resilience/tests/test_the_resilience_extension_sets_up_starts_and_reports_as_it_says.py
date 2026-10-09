"""What the resilience extension installs at setup, opens at start, and reports.

Setup installs a fresh in-memory limiter, or restores the limiter given to the constructor, and scans
the service's limits afresh. `start()` opens a KV limiter's bucket on the service's connection and
leaves an in-memory limiter alone; without a connection it leaves a degraded limiter degraded.
Health names handler limiters only when a handler has its own, and info names the limiter class a
handler declares. A marker that is not a `RateLimitConfig` is not a limit. A message refused for a
missing key leaves one warning bound to the service.
"""

from types import SimpleNamespace

import pytest
from cliffracer_resilience import (
    InMemoryRateLimiter,
    KvRateLimiter,
    ResilienceExtension,
    rate_limit,
)
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import ExtensionSetupContext, RejectMessage, WorkerContext

pytestmark = pytest.mark.unit


# --- fakes ---------------------------------------------------------------------------------


class _Connection:
    is_closed = False


class _Bucket:
    pass


class _JetStream:
    """A JetStream context whose bucket exists; counts opens."""

    def __init__(self) -> None:
        self._nc = _Connection()
        self.opened = 0

    async def key_value(self, name: str) -> _Bucket:
        self.opened += 1
        return _Bucket()

    async def create_key_value(self, bucket: str, **_: object) -> _Bucket:
        return await self.key_value(bucket)


def _context(service: object) -> ExtensionSetupContext:
    return ExtensionSetupContext(
        service_config=getattr(service, "config", None), broker_url="", service=service
    )


def _dispatch(handler: str, headers: dict[str, str] | None = None) -> WorkerContext:
    return WorkerContext(
        kind="rpc",
        subject=f"svc.rpc.{handler}",
        headers=headers or {},
        correlation_id=None,
        payload={},
        data={"handler_name": handler},
    )


# --- setup: the limiter it installs, and the limits it scans ---------------------------------


class _Limited(CliffracerService):
    resilience = ResilienceExtension()

    @rpc
    @rate_limit(calls=1, window=60.0)
    async def create(self) -> int:
        return 1


async def test_setting_up_again_begins_an_in_memory_limiters_windows_empty():
    svc = _Limited(ServiceConfig(name="limited", health_port=0))
    await svc.container._setup_extensions()
    ext = svc.resilience
    await ext.worker_setup(_dispatch("create"))
    await ext.worker_teardown(_dispatch("create"))

    await ext.setup(_context(svc))

    await ext.worker_setup(_dispatch("create"))  # admitted: the window began empty
    assert ext.health_details()["rate_limiter"]["tracked_keys"] == 1


async def test_setup_replaces_a_limiter_assigned_after_the_service_was_built():
    svc = _Limited(ServiceConfig(name="limited", health_port=0))
    await svc.container._setup_extensions()
    assigned = InMemoryRateLimiter()
    svc.resilience.limiter = assigned

    await svc.resilience.setup(_context(svc))

    assert svc.resilience.limiter is not assigned
    assert isinstance(svc.resilience.limiter, InMemoryRateLimiter)


async def test_setup_restores_the_limiter_given_to_the_constructor():
    given = InMemoryRateLimiter()
    ext = ResilienceExtension(limiter=given)
    svc = _Limited(ServiceConfig(name="limited", health_port=0))
    ext.limiter = InMemoryRateLimiter()

    await ext.setup(_context(svc))

    assert ext.limiter is given


async def test_setting_one_extension_up_for_another_service_forgets_the_first_ones_limits():
    class Other(CliffracerService):
        @rpc
        @rate_limit(calls=3, window=10.0)
        async def lookup(self) -> int:
            return 2

    ext = ResilienceExtension()
    await ext.setup(_context(_Limited(ServiceConfig(name="limited", health_port=0))))
    await ext.setup(_context(Other(ServiceConfig(name="other", health_port=0))))

    assert list(ext.info_details()["limits"]) == ["lookup"]


async def test_a_marker_that_is_not_a_rate_limit_config_is_not_taken_for_a_limit():
    class Foreign:
        _cliffracer_rate_limit = {"calls": 1, "window": 1.0}

    class Marked(CliffracerService):
        resilience = ResilienceExtension()
        helper = Foreign()

    svc = Marked(ServiceConfig(name="marked", health_port=0))
    await svc.container._setup_extensions()

    assert svc.resilience.info_details()["limits"] == {}


# --- start: opening a distributed limiter on the service's connection -------------------------


async def test_start_opens_a_kv_limiter_on_the_services_connection():
    limiter = KvRateLimiter(bucket_name="limits")
    ext = ResilienceExtension(limiter=limiter)
    js = _JetStream()
    ext.service = SimpleNamespace(js=js)

    await ext.start()

    assert js.opened == 1
    assert limiter.health_details()["status"] == "distributed"


class _DownBucket:
    """A bucket whose reads fail: a limiter reading it falls back and reports itself degraded."""

    async def get(self, key: str) -> object:
        raise RuntimeError("the store is down")


async def test_start_with_no_connection_leaves_a_degraded_limiter_degraded():
    """A limiter is shared by every service declaring it, so a service starting without a JetStream
    connection must not report its backend recovered."""
    limiter = KvRateLimiter(kv=_DownBucket(), in_memory_fallback=True)
    await limiter.acquire("k", 1, 60.0)
    assert limiter.health_details()["status"] == "degraded"
    ext = ResilienceExtension(limiter=limiter)
    ext.service = SimpleNamespace(js=None)

    await ext.start()

    assert limiter.health_details()["status"] == "degraded"
    assert limiter.health_details()["last_error_type"] == "RuntimeError"


async def test_start_leaves_an_in_memory_limiter_alone_when_the_service_has_a_connection():
    ext = ResilienceExtension()
    ext.service = SimpleNamespace(js=_JetStream())

    await ext.start()  # an in-memory limiter has no bucket to open

    assert ext.health_details()["rate_limiter"]["backend"] == "memory"


# --- health and info ------------------------------------------------------------------------


async def test_health_names_no_handler_limiters_when_no_handler_has_its_own():
    svc = _Limited(ServiceConfig(name="limited", health_port=0))
    await svc.container._setup_extensions()

    assert "handler_rate_limiters" not in svc.resilience.health_details()


async def test_info_names_the_limiter_a_handler_declares_for_itself():
    class Own(CliffracerService):
        resilience = ResilienceExtension()

        @rpc
        @rate_limit(calls=2, window=5.0, limiter=InMemoryRateLimiter())
        async def create(self) -> int:
            return 1

    svc = Own(ServiceConfig(name="own", health_port=0))
    await svc.container._setup_extensions()

    assert svc.resilience.info_details()["limits"]["create"] == {
        "calls": 2,
        "window": 5.0,
        "key": "handler",
        "limiter": "InMemoryRateLimiter",
    }


# --- a refusal for a missing key is logged -------------------------------------------------


async def test_a_message_refused_for_a_missing_key_leaves_a_warning_for_the_service():
    class Keyed(CliffracerService):
        resilience = ResilienceExtension()

        @rpc
        @rate_limit(calls=5, window=60.0, key="x-client")
        async def by_header(self) -> int:
            return 1

    svc = Keyed(ServiceConfig(name="keyed", health_port=0))
    await svc.container._setup_extensions()
    records: list[dict] = []
    sink = logger.add(lambda message: records.append(message.record), level="WARNING")
    try:
        with pytest.raises(RejectMessage) as refused:
            await svc.resilience.worker_setup(_dispatch("by_header"))
    finally:
        logger.remove(sink)

    warnings = [r for r in records if r["extra"].get("service") == "keyed"]
    assert len(warnings) == 1
    assert warnings[0]["level"].name == "WARNING"
    assert str(refused.value) in warnings[0]["message"]
