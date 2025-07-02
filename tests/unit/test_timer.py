"""
Tests for timer functionality
"""

import asyncio
import time

import pytest

from cliffracer import NATSService, ServiceConfig, timer
from cliffracer.core.timer import Timer


class TimerTestService(NATSService):
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
        await service._start_timers()

        # Wait for some executions
        await asyncio.sleep(0.3)

        # Stop timers
        await service._stop_timers()

        # Check that methods were executed
        assert service.tick_count > 0  # fast_tick should have run
        assert service.eager_count > 0  # eager_timer should have run

    async def test_eager_timer_execution(self):
        """Test that eager timers execute immediately"""
        service = TimerTestService()
        service._discover_handlers()

        initial_count = service.eager_count

        # Start timers
        await service._start_timers()

        # Small delay to let eager timer execute
        await asyncio.sleep(0.01)

        # Check that eager timer executed immediately
        assert service.eager_count > initial_count

        await service._stop_timers()

    async def test_timer_interval_accuracy(self):
        """Test that timers execute at approximately correct intervals"""
        service = TimerTestService()
        service._discover_handlers()

        await service._start_timers()

        # Record start time
        start_time = time.time()
        initial_count = len(service.health_checks)

        # Wait for multiple executions
        await asyncio.sleep(0.25)  # Should allow ~5 executions at 0.05s interval

        await service._stop_timers()

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

        await service._start_timers()
        await asyncio.sleep(0.15)
        await service._stop_timers()

        # Both sync and async methods should have incremented tick_count
        # fast_tick (async) adds 1, sync_timer adds 10
        assert service.tick_count >= 10  # At least one sync execution

    async def test_timer_error_handling(self):
        """Test timer error handling"""

        class ErrorService(NATSService):
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

        await service._start_timers()
        await asyncio.sleep(0.2)  # Allow multiple executions
        await service._stop_timers()

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

        await service._start_timers()
        await asyncio.sleep(0.2)
        await service._stop_timers()

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


@pytest.mark.asyncio
class TestTimerPerformance:
    """Test timer performance characteristics"""

    async def test_timer_drift_handling(self):
        """Test that timer handles drift appropriately"""

        class SlowService(NATSService):
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

        await service._start_timers()
        await asyncio.sleep(0.5)
        await service._stop_timers()

        # Should still execute despite slow method
        assert len(service.execution_times) > 0

    async def test_multiple_timers_concurrency(self):
        """Test that multiple timers run concurrently"""

        class MultiTimerService(NATSService):
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

        await service._start_timers()
        await asyncio.sleep(0.25)
        await service._stop_timers()

        # All timers should have executed
        assert service.timer1_count > 0
        assert service.timer2_count > 0
        assert service.timer3_count > 0

        # Faster timer should have executed more times
        assert service.timer1_count >= service.timer2_count
        assert service.timer2_count >= service.timer3_count
