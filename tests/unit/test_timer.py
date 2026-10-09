"""
Tests for timer functionality
"""

import asyncio
import time

import pytest

from cliffracer import CliffracerService, ServiceConfig, timer
from cliffracer.core.timer import Timer
from cliffracer.testing import FakeClock

pytestmark = pytest.mark.unit


async def _start_on(service: CliffracerService, clock: FakeClock) -> None:
    """Start the service's timers reading `clock`, which then waits for each to reach its wait."""
    for t in service.container.registry.timers:
        t.clock = clock
    await service.container._start_timers()
    for t in service.container.registry.timers:
        clock.watch(t.task)


class TimerTestService(CliffracerService):
    """Test service with timer methods"""

    def __init__(self):
        config = ServiceConfig(name="timer_test_service")
        super().__init__(config)
        self.async_ticks = 0
        self.sync_ticks = 0
        self.eager_count = 0
        self.dependency_probes = []

    @timer(interval=0.1)  # 100ms for fast testing
    async def fast_tick(self):
        """Fast timer for testing"""
        self.async_ticks += 1

    @timer(interval=0.2, eager=True)
    async def eager_timer(self):
        """Eager timer that starts immediately"""
        self.eager_count += 1

    @timer(interval=0.1)
    def sync_timer(self):
        """Synchronous timer method"""
        self.sync_ticks += 1

    @timer(interval=0.05)
    async def probe_dependencies(self):
        """Simulated health check"""
        self.dependency_probes.append(time.time())


class TestTimerDecorator:
    """Test timer decorator functionality"""

    def test_timer_clone(self):
        """Timer.clone() creates an independent copy with same config and clean state"""

        def token_fn() -> str:
            return "tok"

        t = Timer(
            interval=1.0,
            eager=True,
            max_drift=0.5,
            error_backoff=2.0,
            headers={"authorization": "Bearer foo"},
            token_factory=token_fn,
        )
        t.method_name = "test_method"
        c = t.clone()
        assert c is not t
        assert c.interval == 1.0
        assert c.eager is True
        assert c.max_drift == 0.5
        assert c.error_backoff == 2.0
        assert c.headers == {"authorization": "Bearer foo"}
        assert c.token_factory is token_fn
        assert c.method_name == "test_method"
        assert c.is_running is False
        assert c.task is None
        assert c.service_instance is None

    def test_a_clone_does_not_share_the_headers_dict_with_the_original(self):
        """The independence the clone's docstring claims, on the one piece of shared mutable
        state. Every service instance gets a clone of the class-level timer, and a firing writes
        its `authorization` header into the dict: shared, one instance's token would be visible
        to, and overwritten by, every other instance of the class."""
        original = Timer(interval=1.0, headers={"authorization": "Bearer foo"})

        clone = original.clone()
        clone.headers["authorization"] = "Bearer someone-else"
        clone.headers["x-added"] = "1"

        assert clone.headers is not original.headers
        assert original.headers == {"authorization": "Bearer foo"}

    def test_a_clone_of_a_timer_with_no_headers_has_none(self):
        assert Timer(interval=1.0).clone().headers is None

    def test_the_decorator_stores_headers_and_token_factory_on_the_timer(self):
        """The decorator hands both to the Timer. What a firing sends is tested
        in `test_a_firing_sends_the_timers_headers_and_bearer_token`."""

        def token_fn() -> str:
            return "tok"

        @timer(interval=5.0, headers={"x-key": "val"}, token_factory=token_fn)
        def test_method():
            pass

        timer_instance = test_method._cliffracer_timers[0]
        assert timer_instance.headers == {"x-key": "val"}
        assert timer_instance.token_factory is token_fn

    def test_timer_decorator_creates_metadata(self):
        """Test that timer decorator adds metadata to methods"""

        @timer(interval=5.0)
        def test_method():
            pass

        assert hasattr(test_method, "_cliffracer_timers")
        assert len(test_method._cliffracer_timers) == 1

        timer_instance = test_method._cliffracer_timers[0]
        assert isinstance(timer_instance, Timer)
        assert timer_instance.interval == 5.0
        assert timer_instance.eager is False

    def test_timer_decorator_with_options(self):
        """Test timer decorator with custom options"""

        @timer(interval=2.5, eager=True, max_drift=0.5)
        def test_method():
            pass

        timer_instance = test_method._cliffracer_timers[0]
        assert timer_instance.interval == 2.5
        assert timer_instance.eager is True
        assert timer_instance.max_drift == 0.5

    def test_multiple_timers_on_method(self):
        """Test multiple timer decorators on same method"""

        @timer(interval=1.0)
        @timer(interval=2.0, eager=True)
        def test_method():
            pass

        assert len(test_method._cliffracer_timers) == 2
        intervals = [t.interval for t in test_method._cliffracer_timers]
        assert 1.0 in intervals
        assert 2.0 in intervals


class TestTimerClass:
    """Test Timer class functionality"""

    @pytest.fixture
    def timer_instance(self):
        """Create a Timer instance for testing"""
        return Timer(interval=0.1, eager=False)

    def test_timer_initialization(self, timer_instance):
        """Test timer initialization"""
        assert timer_instance.interval == 0.1
        assert timer_instance.eager is False
        assert timer_instance.is_running is False
        assert timer_instance.execution_count == 0
        assert timer_instance.error_count == 0

    def test_timer_stats_initial(self, timer_instance):
        """Test initial timer statistics"""
        stats = timer_instance.get_stats()

        assert stats["interval"] == 0.1
        assert stats["eager"] is False
        assert stats["is_running"] is False
        assert stats["execution_count"] == 0
        assert stats["error_count"] == 0
        assert stats["error_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_timer_start_stop(self, timer_instance):
        """Test timer start and stop functionality"""

        # Mock service instance
        class MockService:
            def test_method(self):
                pass

        service = MockService()
        timer_instance.method_name = "test_method"

        # Start timer
        await timer_instance.start(service)
        assert timer_instance.is_running is True
        assert timer_instance.service_instance is service

        # Stop timer
        await timer_instance.stop()
        assert timer_instance.is_running is False

    @pytest.mark.asyncio
    async def test_a_second_start_keeps_the_one_running_loop(self, timer_instance):
        """A second `start()` must not spawn a second loop. `stop()` cancels only
        `self.task`, so a replaced task would be a loop nothing can stop."""

        class MockService:
            def test_method(self):
                pass

        service = MockService()
        timer_instance.method_name = "test_method"

        await timer_instance.start(service)
        first = timer_instance.task
        await timer_instance.start(service)

        loops = [
            task
            for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__qualname__", "") == "Timer._timer_loop"
        ]
        assert timer_instance.task is first
        assert loops == [first]

        await timer_instance.stop()
        assert first.done()

    @pytest.mark.asyncio
    async def test_stop_on_a_timer_never_started_changes_nothing(self, timer_instance):
        await timer_instance.stop()

        assert timer_instance.is_running is False
        assert timer_instance.task is None
        assert not timer_instance._stop_event.is_set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("token_factory", "authorization"),
        [
            (lambda: "abc", "Bearer abc"),
            (lambda: "bearer abc", "bearer abc"),
            (lambda: "", None),
            (None, None),
        ],
        ids=[
            "bare_token_gets_the_prefix",
            "prefixed_token_is_not_prefixed_again",
            "empty_token",
            "no_factory",
        ],
    )
    async def test_a_firing_sends_the_timers_headers_and_bearer_token(
        self, token_factory, authorization
    ):
        """What an auth extension reads: the `WorkerContext` a firing is run in."""
        seen = []

        class Service:
            async def _run_worker(self, ctx, fn):
                seen.append(ctx)
                return await fn()

            async def tick(self):
                return None

        t = Timer(interval=0.1, headers={"x-key": "val"}, token_factory=token_factory)
        t.method_name = "tick"
        t.service_instance = Service()

        await t._execute_method()

        expected = {"x-key": "val"}
        if authorization is not None:
            expected["authorization"] = authorization
        (ctx,) = seen
        assert ctx.kind == "timer"
        assert ctx.headers == expected
        assert t.headers == {"x-key": "val"}, "a firing must not write into the timer's own headers"

    @pytest.mark.asyncio
    async def test_timer_fallback_to_dispatcher_run_worker(self):
        """Test timer runner fallback to dispatcher._run_worker when container has no runner"""
        from unittest.mock import AsyncMock, MagicMock

        from cliffracer.core.dispatcher import MessageDispatcher
        from cliffracer.core.registry import ServiceRegistry

        t = Timer(interval=0.1)
        t.method_name = "tick"

        svc = MagicMock()
        del svc._run_worker
        cfg = ServiceConfig(name="test")
        dispatcher = MessageDispatcher(
            registry=ServiceRegistry(),
            config=cfg,
            connection_provider=lambda: MagicMock(),
            extensions=[],
        )
        called = False

        async def fake_run_worker(ctx, fn):
            nonlocal called
            called = True
            return await fn()

        dispatcher._run_worker = fake_run_worker  # type: ignore[method-assign]
        container = MagicMock(spec=["dispatcher"])
        container.dispatcher = dispatcher
        svc.container = container
        svc.tick = AsyncMock()

        t.service_instance = svc
        await t._execute_method()
        assert called is True
        svc.tick.assert_awaited_once()


@pytest.mark.asyncio
class TestTimerIntegration:
    """Test timer integration with services"""

    async def test_timer_discovery(self):
        """Test that timers are discovered during service initialization"""
        service = TimerTestService()

        # Discover handlers manually (normally done in start())
        service._discover_handlers()

        # Should find all timer-decorated methods
        assert len(service._timers) == 4  # fast_tick, eager_timer, sync_timer, probe_dependencies

        timer_methods = [t.method_name for t in service._timers]
        assert "fast_tick" in timer_methods
        assert "eager_timer" in timer_methods
        assert "sync_timer" in timer_methods
        assert "probe_dependencies" in timer_methods

    async def test_timer_execution(self):
        """Each timer runs once per interval; an eager one also runs at the start."""
        service = TimerTestService()
        service._discover_handlers()
        clock = FakeClock()

        await _start_on(service, clock)
        await clock.advance(0.3)
        await service.container._stop_timers()

        assert service.async_ticks == 3  # fast_tick at 0.1, 0.2 and 0.3
        assert service.eager_count == 2  # eager_timer at 0 and 0.2

    async def test_eager_timer_execution(self):
        """Test that eager timers execute immediately"""
        service = TimerTestService()
        service._discover_handlers()

        initial_count = service.eager_count

        # Start timers
        await service.container._start_timers()

        # Small delay to let eager timer execute
        await asyncio.sleep(0.01)

        # Check that eager timer executed immediately
        assert service.eager_count > initial_count

        await service.container._stop_timers()

    async def test_timer_interval_accuracy(self):
        """A 0.05 s timer runs once per interval: five times in 0.25 s, and not a sixth before
        the sixth interval ends."""
        service = TimerTestService()
        service._discover_handlers()
        clock = FakeClock()

        await _start_on(service, clock)
        await clock.advance(0.25)
        five = len(service.dependency_probes)
        await clock.advance(0.049)
        still_five = len(service.dependency_probes)
        await clock.advance(0.001)
        await service.container._stop_timers()

        assert (five, still_five, len(service.dependency_probes)) == (5, 5, 6)

    async def test_sync_and_async_timers(self):
        """Both a sync and an async timer method run, each counted on its own.

        One shared counter let a single sync firing satisfy the assertion, so
        async methods could stop running with this test green.
        """
        service = TimerTestService()
        service._discover_handlers()

        clock = FakeClock()
        await _start_on(service, clock)
        await clock.advance(0.15)
        await service.container._stop_timers()

        assert service.sync_ticks == 1
        assert service.async_ticks == 1

    async def test_timer_error_handling(self):
        """Test timer error handling"""

        class ErrorService(CliffracerService):
            def __init__(self):
                config = ServiceConfig(name="error_service")
                super().__init__(config)
                self.error_count = 0

            @timer(interval=0.05)
            async def failing_timer(self):
                self.error_count += 1
                if self.error_count <= 2:
                    raise ValueError("Test error")
                # Succeed after 2 failures

        service = ErrorService()
        service._discover_handlers()

        clock = FakeClock()
        await _start_on(service, clock)
        await clock.advance(0.2)  # four intervals: two failing firings, then two that succeed
        await service.container._stop_timers()

        # Should have continued executing despite errors
        assert service.error_count == 4

        # Check timer error statistics
        timer_instance = service._timers[0]
        stats = timer_instance.get_stats()
        assert stats["error_count"] > 0
        assert stats["execution_count"] > stats["error_count"]

    async def test_timer_stats_collection(self):
        """Test timer statistics collection"""
        service = TimerTestService()
        service._discover_handlers()

        clock = FakeClock()
        await _start_on(service, clock)
        await clock.advance(0.2)
        await service.container._stop_timers()

        # Get service timer stats
        service_stats = service.get_timer_stats()
        assert service_stats["timer_count"] == 4
        assert len(service_stats["timers"]) == 4

        stats_by_name = {t["method_name"]: t for t in service_stats["timers"]}
        assert "probe_dependencies" in stats_by_name
        assert "fast_tick" in stats_by_name
        assert "eager_timer" in stats_by_name
        assert "sync_timer" in stats_by_name

        # Check individual timer stats with positive counter assertions
        for timer_stats in service_stats["timers"]:
            assert "execution_count" in timer_stats
            assert "error_count" in timer_stats
            assert "interval" in timer_stats
            assert timer_stats["error_count"] == 0
            assert timer_stats["execution_count"] > 0
            assert timer_stats["average_execution_time"] >= 0.0

        assert stats_by_name["probe_dependencies"]["execution_count"] == 4
        assert stats_by_name["eager_timer"]["execution_count"] == 2
        assert stats_by_name["fast_tick"]["execution_count"] == 2
        assert stats_by_name["sync_timer"]["execution_count"] == 2

    async def test_service_info_includes_timers(self):
        """Test that service info includes timer methods"""
        service = TimerTestService()
        service._discover_handlers()

        service_info = service.get_service_info()

        assert "timer_methods" in service_info
        timer_methods = service_info["timer_methods"]
        assert "fast_tick" in timer_methods
        assert "eager_timer" in timer_methods
        assert "sync_timer" in timer_methods
        assert "probe_dependencies" in timer_methods

    async def test_two_instances_of_same_service_class_have_independent_timers(self):
        """Verify multiple instances of same service class do not share Timer objects."""

        class MultiInstanceTimerService(CliffracerService):
            def __init__(self, name: str):
                super().__init__(ServiceConfig(name=name))
                self.hits = 0

            @timer(interval=0.05)
            async def tick(self):
                self.hits += 1

        a = MultiInstanceTimerService("svc_a")
        b = MultiInstanceTimerService("svc_b")

        a._discover_handlers()
        b._discover_handlers()

        # Timers must not be the same instance
        assert a._timers[0] is not b._timers[0]

        clock = FakeClock()
        await _start_on(a, clock)
        await _start_on(b, clock)

        await clock.advance(0.15)
        assert (a.hits, b.hits) == (3, 3)

        # Stop b; a should keep running
        await b.container._stop_timers()

        await clock.advance(0.15)
        assert (a.hits, b.hits) == (6, 3)

        await a.container._stop_timers()


@pytest.mark.asyncio
class TestTimerPerformance:
    """Test timer performance characteristics"""

    async def test_multiple_timers_concurrency(self):
        """Test that multiple timers run concurrently"""

        class MultiTimerService(CliffracerService):
            def __init__(self):
                config = ServiceConfig(name="multi_timer_service")
                super().__init__(config)
                self.timer1_count = 0
                self.timer2_count = 0
                self.timer3_count = 0

            @timer(interval=0.05)
            async def timer1(self):
                self.timer1_count += 1

            @timer(interval=0.07)
            async def timer2(self):
                self.timer2_count += 1

            @timer(interval=0.11)
            async def timer3(self):
                self.timer3_count += 1

        service = MultiTimerService()
        service._discover_handlers()

        clock = FakeClock()
        await _start_on(service, clock)
        await clock.advance(0.25)
        await service.container._stop_timers()

        # Each ran once per interval of its own in the same 0.25 s.
        counts = (service.timer1_count, service.timer2_count, service.timer3_count)
        assert counts == (5, 3, 2)


# --- what the timer loop decides, on a clock it cannot outrun -----------------
#
# `_timer_loop` reads time and waits through its clock. The real clock reads
# `time.monotonic()` and waits with `asyncio.wait_for` and `asyncio.sleep`, all
# through names in the `cliffracer.core.clock` module.
# These tests replace those two names in that module only: waits and sleeps are
# recorded and advance a fake clock instead of passing, so each test reads the
# loop's decision -- how long it chose to wait -- exactly, whatever the host
# load. Nothing outside the module is patched; a session fixture tearing down
# at the same time still has the real asyncio.
#
# Each test asserts the exact waits (and sleeps) it recorded. A loop that
# stopped routing through the module's names would record none and red them:
# with `wait_for` reached as `__import__("asyncio").wait_for` instead, all three
# exact-waits tests below go red.


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _LoopAsyncio:
    """Stands in for `asyncio` inside `cliffracer.core.clock`, which a timer's real clock waits
    through."""

    def __init__(self, clock: _FakeClock, real_asyncio) -> None:
        self._clock = clock
        self._real = real_asyncio
        self.waits: list[float] = []
        self.sleeps: list[float] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def wait_for(self, awaitable, timeout):
        awaitable.close()
        self.waits.append(timeout)
        self._clock.now += timeout
        raise TimeoutError

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self._clock.now += seconds


def _install_clock(monkeypatch):
    import cliffracer.core.clock as clock_module

    clock = _FakeClock()
    loop_asyncio = _LoopAsyncio(clock, clock_module.asyncio)
    monkeypatch.setattr(clock_module, "time", clock)
    monkeypatch.setattr(clock_module, "asyncio", loop_asyncio)
    return clock, loop_asyncio


def _loop_timer(firings, *, interval=1.0, **kwargs) -> Timer:
    """A Timer whose loop runs exactly `len(firings)` firings, each a callable
    taking the clock, then stops."""
    t = Timer(interval=interval, **kwargs)
    t.method_name = "tick"
    t.is_running = True
    remaining = list(firings)

    async def execute():
        step = remaining.pop(0)
        if not remaining:
            t.is_running = False
        step()

    t._execute_method = execute  # type: ignore[method-assign]
    return t


class TestTheTimerLoopsDecisions:
    async def test_the_loop_waits_its_interval_before_the_first_firing(self):
        """A 30 second interval has not fired 1 µs before 30 s on its clock, and has fired once
        at 30 s: the loop does not fire before its first interval, nor after it."""
        clock = FakeClock()
        fired: list[float] = []
        t = _loop_timer([lambda: fired.append(clock.monotonic())], interval=30.0, clock=clock)
        task = asyncio.create_task(t._timer_loop())
        clock.watch(task)

        await clock.advance(30.0 - 1e-6)
        assert fired == [] and not task.done()
        await clock.advance(1e-6)
        assert fired == [30.0]
        await task

    async def test_an_overrun_past_max_drift_rebases_the_schedule(self, monkeypatch):
        """A firing that overruns by more than `max_drift` puts the next firing a
        full interval after now, instead of firing again at once to catch up."""
        clock, loop = _install_clock(monkeypatch)

        def overrun():
            clock.now += 3.0

        t = _loop_timer([overrun, lambda: None], interval=1.0, max_drift=0.5)

        await t._timer_loop()

        assert loop.waits == [pytest.approx(1.0), pytest.approx(1.0)]

    async def test_CONTROL_an_overrun_within_max_drift_keeps_the_schedule(self, monkeypatch):
        clock, loop = _install_clock(monkeypatch)

        def short_overrun():
            clock.now += 0.2

        t = _loop_timer([short_overrun, lambda: None], interval=1.0, max_drift=0.5)

        await t._timer_loop()

        assert loop.waits == [pytest.approx(1.0), pytest.approx(0.8)]

    async def test_an_overrun_past_the_next_firing_within_max_drift_catches_up(self, monkeypatch):
        """1.3 s of work on a 1 s interval with `max_drift` 0.5: the next firing is 0.3 s late,
        within the drift, so the loop fires it at once instead of waiting a full interval."""
        clock, loop = _install_clock(monkeypatch)

        def overrun_within_drift():
            clock.now += 1.3

        t = _loop_timer([overrun_within_drift, lambda: None], interval=1.0, max_drift=0.5)

        await t._timer_loop()

        assert loop.waits == [pytest.approx(1.0)]

    async def test_a_loop_error_backs_off_and_rebases_the_schedule(self, monkeypatch):
        """An exception that escapes `_execute_method` -- its method lookup and the
        context clear run outside its own try -- is counted, backed off for
        `error_backoff`, and the next firing is scheduled an interval later."""
        _, loop = _install_clock(monkeypatch)

        def lookup_fails():
            raise RuntimeError("raised before _execute_method's own try")

        t = _loop_timer([lookup_fails, lambda: None], interval=1.0, error_backoff=0.25)

        await t._timer_loop()

        assert t.error_count == 1
        # The backoff waits on the stop event, so it is a wait: the first interval, the backoff,
        # then the next interval.
        assert loop.sleeps == []
        assert loop.waits == [pytest.approx(1.0), 0.25, pytest.approx(1.0)]


# --- an eager firing that raises is handled like any firing ------------------


class _LookupRaises:
    """A service whose timer method cannot be read: `_execute_method` reads it
    with getattr before its own try, so the exception reaches the loop."""

    @property
    def tick(self):
        raise RuntimeError("lookup raised")


class TestAnEagerFiringThatRaises:
    async def test_it_is_counted_backed_off_and_the_schedule_rebased(self, monkeypatch):
        """As a scheduled firing's error is. Rebased: the first scheduled firing is
        an interval after the backoff, not an interval after the start."""
        _, loop = _install_clock(monkeypatch)

        def lookup_fails():
            raise RuntimeError("raised before _execute_method's own try")

        t = _loop_timer([lookup_fails, lambda: None], interval=1.0, eager=True, error_backoff=0.25)

        await t._timer_loop()

        assert t.error_count == 1
        assert loop.sleeps == []
        assert loop.waits == [0.25, pytest.approx(1.0)]

    async def test_a_started_timer_survives_it(self):
        """The real task: it used to end with the exception while `is_running`
        stayed True and nothing was logged or counted."""
        clock = FakeClock()
        t = Timer(interval=0.05, eager=True, error_backoff=0.01, clock=clock)
        t.method_name = "tick"
        await t.start(_LookupRaises())
        clock.watch(t.task)
        try:
            # The eager error, its 0.01 s backoff, then the first scheduled firing 0.05 s later.
            await clock.advance(0.06)
            assert not t.task.done(), t.task
            assert t.error_count == 2, "the eager error and the first scheduled one"
        finally:
            await t.stop()

    async def test_CONTROL_a_started_timer_that_is_not_eager_survives_the_same_service(self):
        clock = FakeClock()
        t = Timer(interval=0.05, eager=False, error_backoff=0.01, clock=clock)
        t.method_name = "tick"
        await t.start(_LookupRaises())
        clock.watch(t.task)
        try:
            await clock.advance(0.05)
            assert not t.task.done(), t.task
            assert t.error_count == 1
        finally:
            await t.stop()
