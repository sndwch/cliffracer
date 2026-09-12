"""Tests verifying correlation IDs are scoped per timer execution."""

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.timer import Timer

pytestmark = pytest.mark.unit


class _Recorder:
    """Stands in for a service whose timer method starts an RPC."""

    def __init__(self) -> None:
        self.seen: list[str | None] = []

    async def tick(self) -> None:
        # Exactly what call_rpc does to pick an ID.
        cid = CorrelationContext.get() or CorrelationContext.get_or_create_id()
        CorrelationContext.set(cid)
        self.seen.append(cid)


class TestTimerScoping:
    @pytest.mark.asyncio
    async def test_each_firing_gets_a_distinct_correlation_id(self):
        rec = _Recorder()
        timer = Timer(interval=0.01)
        timer.method_name = "tick"
        timer.service_instance = rec
        for _ in range(5):
            await timer._execute_method()

        assert len(rec.seen) == 5
        assert len(set(rec.seen)) == 5, (
            "each timer firing is a new root operation and must get its own "
            f"correlation ID; got {rec.seen}"
        )

    @pytest.mark.asyncio
    async def test_the_id_does_not_leak_out_of_the_firing(self):
        """After a firing completes, the context must not still hold its ID.

        Otherwise the next firing — or anything else sharing the task — starts
        already correlated to a finished operation.
        """
        rec = _Recorder()
        timer = Timer(interval=0.01)
        timer.method_name = "tick"
        timer.service_instance = rec
        CorrelationContext.clear()
        await timer._execute_method()
        assert CorrelationContext.get() is None


class TestInheritanceStillWorks:
    """Tests for downstream correlation ID propagation within timer executions."""

    @pytest.mark.asyncio
    async def test_an_explicitly_set_id_is_visible_inside_the_firing(self):
        """A firing starts clean, but work it causes inherits ITS id."""
        seen_inner: list[str | None] = []

        class Nested:
            async def tick(self) -> None:
                outer = CorrelationContext.get_or_create_id()
                CorrelationContext.set(outer)

                async def caused_by_this_firing():
                    seen_inner.append(CorrelationContext.get())

                await caused_by_this_firing()

        timer = Timer(interval=0.01)
        timer.method_name = "tick"
        timer.service_instance = Nested()
        await timer._execute_method()

        assert seen_inner and seen_inner[0] is not None, (
            "work caused by a firing must inherit that firing's correlation ID"
        )

    @pytest.mark.asyncio
    async def test_concurrent_firings_do_not_share_ids(self):
        rec_a, rec_b = _Recorder(), _Recorder()
        ta = Timer(interval=0.01)
        ta.method_name = "tick"
        ta.service_instance = rec_a
        tb = Timer(interval=0.01)
        tb.method_name = "tick"
        tb.service_instance = rec_b

        await asyncio.gather(ta._execute_method(), tb._execute_method())

        assert rec_a.seen[0] != rec_b.seen[0], (
            "two timers firing concurrently must not share a correlation ID"
        )


class _FailingService(CliffracerService):
    """Service double with active container hook chain for failure testing."""

    async def boom(self) -> None:
        CorrelationContext.set(CorrelationContext.get() or CorrelationContext.get_or_create_id())
        raise RuntimeError("this firing failed")


class TestTimerScopingOnARealService:
    @pytest.mark.asyncio
    async def test_a_FAILED_firing_does_not_leak_its_id(self):
        """Ensure failed timer firings do not leak correlation IDs outside execution scope."""
        svc = _FailingService(ServiceConfig(name="t"))
        await svc.container._setup_extensions()
        timer = Timer(interval=0.01)
        timer.method_name = "boom"
        timer.service_instance = svc

        CorrelationContext.clear()
        await timer._execute_method()

        assert CorrelationContext.get() is None, (
            "a firing that raised left its correlation ID in the context; the "
            "next thing sharing this task starts correlated to a failed operation"
        )
