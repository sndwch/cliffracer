"""Verify correlation ID scoping for cron task executions."""

import pytest
from cliffracer_cron import CronTimer

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.correlation import CorrelationContext

pytestmark = pytest.mark.unit


class _Recorder:
    """Record correlation IDs across invocations."""

    def __init__(self) -> None:
        self.seen: list[str | None] = []

    async def tick(self) -> None:
        # Exactly what call_rpc does to pick an ID.
        cid = CorrelationContext.get() or CorrelationContext.get_or_create_id()
        CorrelationContext.set(cid)
        self.seen.append(cid)


class TestCronScoping:
    @pytest.mark.asyncio
    async def test_each_firing_gets_a_distinct_correlation_id(self):
        """Verify each cron execution generates a distinct correlation ID."""
        rec = _Recorder()
        cron = CronTimer(expression="* * * * *")
        cron.method_name = "tick"
        cron.service_instance = rec
        for _ in range(5):
            await cron._execute_method()

        assert len(set(rec.seen)) == 5, (
            f"each cron firing must get its own correlation ID; got {rec.seen}"
        )


class _FailingService(CliffracerService):
    """Service configured with extensions to verify context cleanup on errors."""

    async def boom(self) -> None:
        CorrelationContext.set(CorrelationContext.get() or CorrelationContext.get_or_create_id())
        raise RuntimeError("this firing failed")


class TestCronScopingOnARealService:
    @pytest.mark.asyncio
    async def test_a_FAILED_firing_does_not_leak_its_id(self):
        """Verify failed cron executions do not leak correlation context."""
        svc = _FailingService(ServiceConfig(name="t"))
        await svc.container._setup_extensions()
        cron = CronTimer(expression="* * * * *")
        cron.method_name = "boom"
        cron.service_instance = svc

        CorrelationContext.clear()
        await cron._execute_method()

        assert CorrelationContext.get() is None, (
            "a cron firing that raised left its correlation ID in the context"
        )
