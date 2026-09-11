#!/usr/bin/env python3
"""
Timer + PerformanceMetrics Integration Example

This example shows how timers automatically integrate with Cliffracer's
PerformanceMetrics system for monitoring and observability.
"""

import asyncio
import time

from cliffracer_metrics import MetricsExtension

from cliffracer import CliffracerService, ServiceConfig, timer


class MetricsTimerService(CliffracerService):
    """
    Service demonstrating timer + metrics integration.

    MetricsExtension records dispatch timing for all handlers, including timers.
    """

    metrics = MetricsExtension()

    def __init__(self):
        config = ServiceConfig(name="metrics_timer_service")
        super().__init__(config)

        self.task_count = 0
        self.errors_injected = 0

    @timer(interval=2)  # Every 2 seconds
    async def fast_task(self):
        """Fast task that completes quickly"""
        await asyncio.sleep(0.01)  # 10ms task
        self.task_count += 1
        print(f"[OK] Fast task #{self.task_count} completed")

    @timer(interval=3)  # Every 3 seconds
    async def slow_task(self):
        """Slower task to show duration metrics"""
        await asyncio.sleep(0.5)  # 500ms task
        print("[INFO] Slow task completed in 500ms")

    @timer(interval=4, eager=True)  # Every 4 seconds, start immediately
    async def error_prone_task(self):
        """Task that occasionally fails to demonstrate error metrics"""
        self.errors_injected += 1

        if self.errors_injected % 3 == 0:  # Fail every 3rd execution
            print(f"[ERROR] Error-prone task #{self.errors_injected} - injecting error")
            raise ValueError(f"Simulated error #{self.errors_injected}")
        else:
            await asyncio.sleep(0.1)
            print(f"[OK] Error-prone task #{self.errors_injected} succeeded")

    @timer(interval=5)  # Every 5 seconds
    def sync_task(self):
        """Synchronous task to show sync timer metrics"""
        time.sleep(0.05)  # 50ms blocking task
        print("[INFO] Sync task completed in 50ms")


async def run_metrics_demo():
    """
    Run the metrics demo showing timer integration
    """
    print("[INFO] Timer + PerformanceMetrics Integration Demo")
    print("=" * 55)

    service = MetricsTimerService()

    try:
        print("[INFO] Starting service with metrics enabled...")
        await service.start()

        print("[INFO]  Timers are now running and collecting metrics...")
        print()

        # Let timers run for a while
        for i in range(6):
            await asyncio.sleep(5)

            # The extension contributes this to /health too, under its
            # declared attribute name ("metrics").
            metrics = service.metrics.health_details() or {}

            print(f"\n[METRICS] Metrics Report #{i + 1}:")
            print("-" * 30)

            for kind, stats in sorted(metrics.items()):
                print(f"{kind}: {stats['count']} dispatched, {stats['errors']} errored")
                print(
                    f"  latency max {stats['latency_ms']['max']:.2f}ms "
                    f"avg {stats['latency_ms']['avg']:.2f}ms"
                )

        print("\n[INFO] Final Timer Statistics:")
        print("=" * 35)

        timer_stats = service.get_timer_stats()
        for timer_info in timer_stats["timers"]:
            print(f"\n[INFO] {timer_info['method_name']}:")
            print(f"  • Executions: {timer_info['execution_count']}")
            print(f"  • Errors: {timer_info['error_count']}")
            print(f"  • Error Rate: {timer_info['error_rate']:.1f}%")
            print(f"  • Avg Duration: {timer_info['average_execution_time']:.3f}s")
            print(f"  • Total Runtime: {timer_info['total_execution_time']:.2f}s")

        print("\n[INFO] Timer Metrics Integration Features:")
        print("  • Automatic execution counting")
        print("  • Duration tracking in milliseconds")
        print("  • Error rate monitoring")
        print("  • Zero-overhead when metrics disabled")
        print("  • Integrates with PerformanceMetrics system")

    except KeyboardInterrupt:
        print("\n[STOP]  Demo interrupted")
    finally:
        await service.stop()
        print("[OK] Service stopped")


if __name__ == "__main__":
    asyncio.run(run_metrics_demo())
