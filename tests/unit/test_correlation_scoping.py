"""Tests verifying correlation IDs are scoped per timer execution."""

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.timer import Timer
from cliffracer.testing import FakeClock

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


AMBIENT = "corr_fromservicestartup"


class TestAFiringDoesNotInheritWhatWasAmbient:
    """A firing is a new root operation, so an id already in the context is not its id.

    The timer's loop is a task, and a task starts with a copy of the context that created it: a
    service started under a request's correlation id would otherwise stamp every firing with it.
    """

    @pytest.mark.asyncio
    async def test_a_firing_run_under_an_ambient_id_gets_its_own(self):
        rec = _Recorder()
        timer = Timer(interval=0.01)
        timer.method_name = "tick"
        timer.service_instance = rec

        CorrelationContext.set(AMBIENT)
        try:
            await timer._execute_method()
        finally:
            CorrelationContext.clear()

        assert len(rec.seen) == 1
        assert rec.seen[0] != AMBIENT, "the firing carried the id that was ambient before it"
        assert (rec.seen[0] or "").startswith("corr_")

    @pytest.mark.asyncio
    async def test_the_first_firing_of_a_timer_started_under_an_ambient_id_gets_its_own(self):
        """The loop task copied the id when it was created, and only the firing can clear it.

        Eager, so the first firing is the loop's own first act: nothing has cleared its copy yet.
        """
        rec = _Recorder()
        clock = FakeClock()
        timer = Timer(interval=0.01, eager=True, clock=clock)
        timer.method_name = "tick"

        CorrelationContext.set(AMBIENT)
        try:
            await timer.start(rec)
            clock.watch(timer.task)
            CorrelationContext.clear()  # the creator moves on; the loop's copy still holds it
            await clock.advance(0.02)  # the eager firing and two scheduled ones
            assert len(rec.seen) == 3, rec.seen
        finally:
            await timer.stop()
            CorrelationContext.clear()

        assert AMBIENT not in rec.seen, rec.seen
        assert len(set(rec.seen)) == len(rec.seen), rec.seen


class TestInheritanceStillWorks:
    """Tests for downstream correlation ID propagation within timer executions."""

    @pytest.mark.asyncio
    async def test_an_explicitly_set_id_is_visible_inside_the_firing(self):
        """A firing starts clean, but work it causes inherits ITS id."""
        seen_inner: list[str | None] = []
        firing_ids: list[str] = []

        class Nested:
            async def tick(self) -> None:
                outer = CorrelationContext.get_or_create_id()
                firing_ids.append(outer)
                CorrelationContext.set(outer)

                async def caused_by_this_firing():
                    seen_inner.append(CorrelationContext.get())

                await caused_by_this_firing()

        timer = Timer(interval=0.01)
        timer.method_name = "tick"
        timer.service_instance = Nested()
        await timer._execute_method()

        assert seen_inner and firing_ids, "the firing did not run"
        assert seen_inner[0] == firing_ids[0], (
            "work caused by a firing must inherit that firing's correlation ID, not some other id: "
            f"{seen_inner[0]!r} != {firing_ids[0]!r}"
        )
        assert firing_ids[0].startswith("corr_")

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

    fired: list[str | None]

    async def boom(self) -> None:
        CorrelationContext.set(CorrelationContext.get() or CorrelationContext.get_or_create_id())
        self.fired.append(CorrelationContext.get())
        raise RuntimeError("this firing failed")


class TestTimerScopingOnARealService:
    @pytest.mark.asyncio
    async def test_a_FAILED_firing_does_not_leak_its_id(self):
        """Ensure failed timer firings do not leak correlation IDs outside execution scope."""
        svc = _FailingService(ServiceConfig(name="t"))
        svc.fired = []
        await svc.container._setup_extensions()
        timer = Timer(interval=0.01)
        timer.method_name = "boom"
        timer.service_instance = svc

        CorrelationContext.clear()
        await timer._execute_method()

        # The firing happened, set an id, and failed: a firing that never ran also leaves the
        # context empty, so the assertion below is only read once these hold.
        assert len(svc.fired) == 1 and svc.fired[0] is not None, svc.fired
        assert timer.error_count == 1
        assert (timer.last_error or "").startswith("RuntimeError"), timer.last_error

        assert CorrelationContext.get() is None, (
            "a firing that raised left its correlation ID in the context; the "
            "next thing sharing this task starts correlated to a failed operation"
        )


class _Probe(CliffracerService):
    """A real service whose timer method only READS the ambient id.

    Unlike `_Recorder`, it does not pick or set an id itself, so a firing sees one only if the
    service's own hook chain (`CorrelationExtension`) stamped it.
    """

    seen: list[str | None]

    async def probe(self) -> None:
        self.seen.append(CorrelationContext.get())


class TestAFiringOnARealServiceIsStampedByTheHookChain:
    async def _fire(self, times: int) -> _Probe:
        svc = _Probe(ServiceConfig(name="probe"))
        svc.seen = []
        await svc.container._setup_extensions()
        timer = Timer(interval=0.01)
        timer.method_name = "probe"
        timer.service_instance = svc
        for _ in range(times):
            CorrelationContext.clear()
            await timer._execute_method()
        return svc

    @pytest.mark.asyncio
    async def test_each_firing_sees_an_id_it_was_not_given(self):
        svc = await self._fire(3)

        assert len(svc.seen) == 3
        assert all(cid for cid in svc.seen), svc.seen

    @pytest.mark.asyncio
    async def test_the_ids_of_separate_firings_differ(self):
        svc = await self._fire(3)

        assert len(set(svc.seen)) == 3, svc.seen

    @pytest.mark.asyncio
    async def test_the_stamped_id_is_gone_after_the_firing(self):
        await self._fire(1)

        assert CorrelationContext.get() is None
