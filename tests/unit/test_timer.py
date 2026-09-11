"""
Tests for timer functionality
"""

import asyncio
import time

import pytest

from cliffracer import CliffracerService, ServiceConfig, timer
from cliffracer.core.timer import Timer


class TimerTestService(CliffracerService):
    """Test service with timer methods"""

    def __init__(self):
        config = ServiceConfig(name="timer_test_service")
        super().__init__(config)
        self.tick_count = 0
        self.eager_count = 0
        self.health_checks = []

    @timer(interval=0.1)  # 100ms for fast testing
    async def fast_tick(self):
        """Fast timer for testing"""
        self.tick_count += 1

    @timer(interval=0.2, eager=True)
    async def eager_timer(self):
        """Eager timer that starts immediately"""
        self.eager_count += 1

    @timer(interval=0.1)
    def sync_timer(self):
        """Synchronous timer method"""
        self.tick_count += 10

    @timer(interval=0.05)
    async def health_check(self):
        """Simulated health check"""
        self.health_checks.append(time.time())


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

    def test_timer_decorator_passes_headers_and_token_factory(self):
        """Test timer decorator with headers and token_factory"""

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
        assert len(service._timers) == 4  # fast_tick, eager_timer, sync_timer, health_check

        timer_methods = [t.method_name for t in service._timers]
        assert "fast_tick" in timer_methods
        assert "eager_timer" in timer_methods
        assert "sync_timer" in timer_methods
        assert "health_check" in timer_methods

    async def test_timer_execution(self):
        """Test that timers execute their methods"""
        service = TimerTestService()
        service._discover_handlers()

        # Start timers
        await service.container._start_timers()

        # Wait for some executions
        await asyncio.sleep(0.3)

        # Stop timers
        await service.container._stop_timers()

        # Check that methods were executed
        assert service.tick_count > 0  # fast_tick should have run
        assert service.eager_count > 0  # eager_timer should have run

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
        """Test that timers execute at approximately correct intervals"""
        service = TimerTestService()
        service._discover_handlers()

        await service.container._start_timers()

        # Record start time
        start_time = time.time()
        initial_count = len(service.health_checks)

        # Wait for multiple executions
        await asyncio.sleep(0.25)  # Should allow ~5 executions at 0.05s interval

        await service.container._stop_timers()

        # Check timing
        execution_count = len(service.health_checks) - initial_count
        elapsed_time = time.time() - start_time

        # Should have executed approximately every 0.05 seconds
        # Allow for some timing variance
        expected_executions = elapsed_time / 0.05
        assert abs(execution_count - expected_executions) < 2

    async def test_sync_and_async_timers(self):
        """Test that both sync and async timer methods work"""
        service = TimerTestService()
        service._discover_handlers()

        await service.container._start_timers()
        await asyncio.sleep(0.15)
        await service.container._stop_timers()

        # Both sync and async methods should have incremented tick_count
        # fast_tick (async) adds 1, sync_timer adds 10
        assert service.tick_count >= 10  # At least one sync execution

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

        await service.container._start_timers()
        await asyncio.sleep(0.2)  # Allow multiple executions
        await service.container._stop_timers()

        # Should have continued executing despite errors
        assert service.error_count > 2

        # Check timer error statistics
        timer_instance = service._timers[0]
        stats = timer_instance.get_stats()
        assert stats["error_count"] > 0
        assert stats["execution_count"] > stats["error_count"]

    async def test_timer_stats_collection(self):
        """Test timer statistics collection"""
        service = TimerTestService()
        service._discover_handlers()

        await service.container._start_timers()
        await asyncio.sleep(0.2)
        await service.container._stop_timers()

        # Get service timer stats
        service_stats = service.get_timer_stats()
        assert service_stats["timer_count"] == 4
        assert len(service_stats["timers"]) == 4

        # Check individual timer stats
        for timer_stats in service_stats["timers"]:
            assert "execution_count" in timer_stats
            assert "error_count" in timer_stats
            assert "interval" in timer_stats
            assert timer_stats["execution_count"] >= 0

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
        assert "health_check" in timer_methods

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

        await a.container._start_timers()
        await b.container._start_timers()

        await asyncio.sleep(0.15)
        assert a.hits > 0
        assert b.hits > 0

        # Stop b; a should keep running
        await b.container._stop_timers()
        b_hits = b.hits
        a_hits_1 = a.hits

        await asyncio.sleep(0.15)
        assert b.hits == b_hits
        assert a.hits > a_hits_1

        await a.container._stop_timers()


@pytest.mark.asyncio
class TestTimerPerformance:
    """Test timer performance characteristics"""

    async def test_timer_drift_handling(self):
        """Test that timer handles drift appropriately"""

        class SlowService(CliffracerService):
            def __init__(self):
                config = ServiceConfig(name="slow_service")
                super().__init__(config)
                self.execution_times = []

            @timer(interval=0.1, max_drift=0.05)
            async def slow_method(self):
                start_time = time.time()
                await asyncio.sleep(0.15)  # Longer than interval
                self.execution_times.append(start_time)

        service = SlowService()
        service._discover_handlers()

        await service.container._start_timers()
        await asyncio.sleep(0.5)
        await service.container._stop_timers()

        # Should still execute despite slow method
        assert len(service.execution_times) > 0

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

        await service.container._start_timers()
        await asyncio.sleep(0.25)
        await service.container._stop_timers()

        # All timers should have executed
        assert service.timer1_count > 0
        assert service.timer2_count > 0
        assert service.timer3_count > 0

        # Faster timer should have executed more times
        assert service.timer1_count >= service.timer2_count
        assert service.timer2_count >= service.timer3_count
