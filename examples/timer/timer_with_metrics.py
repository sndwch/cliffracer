#!/usr/bin/env python3
"""
Timer + PerformanceMetrics Integration Example

This example shows how timers automatically integrate with Cliffracer's 
PerformanceMetrics system for monitoring and observability.
"""

import asyncio
import time
from cliffracer import HighPerformanceService, ServiceConfig, timer


class MetricsTimerService(HighPerformanceService):
    """
    Service demonstrating timer + metrics integration
    """
    
    def __init__(self):
        config = ServiceConfig(name="metrics_timer_service")
        super().__init__(
            config,
            enable_connection_pooling=True,
            enable_batch_processing=True, 
            enable_metrics=True  # Enable PerformanceMetrics
        )
        
        self.task_count = 0
        self.errors_injected = 0
    
    @timer(interval=2)  # Every 2 seconds
    async def fast_task(self):
        """Fast task that completes quickly"""
        await asyncio.sleep(0.01)  # 10ms task
        self.task_count += 1
        print(f"✅ Fast task #{self.task_count} completed")
    
    @timer(interval=3)  # Every 3 seconds  
    async def slow_task(self):
        """Slower task to show duration metrics"""
        await asyncio.sleep(0.5)  # 500ms task
        print(f"🐌 Slow task completed in 500ms")
    
    @timer(interval=4, eager=True)  # Every 4 seconds, start immediately
    async def error_prone_task(self):
        """Task that occasionally fails to demonstrate error metrics"""
        self.errors_injected += 1
        
        if self.errors_injected % 3 == 0:  # Fail every 3rd execution
            print(f"❌ Error-prone task #{self.errors_injected} - injecting error")
            raise ValueError(f"Simulated error #{self.errors_injected}")
        else:
            await asyncio.sleep(0.1)
            print(f"✅ Error-prone task #{self.errors_injected} succeeded")
    
    @timer(interval=5)  # Every 5 seconds
    def sync_task(self):
        """Synchronous task to show sync timer metrics"""
        time.sleep(0.05)  # 50ms blocking task
        print(f"⚡ Sync task completed in 50ms")


async def run_metrics_demo():
    """
    Run the metrics demo showing timer integration
    """
    print("🎯 Timer + PerformanceMetrics Integration Demo")
    print("=" * 55)
    
    service = MetricsTimerService()
    
    try:
        print("🚀 Starting service with metrics enabled...")
        await service.start()
        
        print("⏱️  Timers are now running and collecting metrics...")
        print()
        
        # Let timers run for a while
        for i in range(6):
            await asyncio.sleep(5)
            
            # Get comprehensive metrics every 5 seconds
            metrics = service.get_performance_metrics()
            
            print(f"\n📊 Metrics Report #{i+1}:")
            print("-" * 30)
            
            # Timer-specific metrics (automatically collected)
            custom_metrics = metrics.get("custom", {})
            counters = custom_metrics.get("counters", {})
            
            timer_metrics = {k: v for k, v in counters.items() if k.startswith("timer_")}
            
            if timer_metrics:
                print("Timer Execution Counts:")
                for metric_name, count in timer_metrics.items():
                    print(f"  {metric_name}: {count}")
            
            # Overall performance metrics
            if "latency" in metrics:
                latency = metrics["latency"]
                if "count" in latency:
                    print(f"Total Operations: {latency['count']}")
                    print(f"Mean Latency: {latency.get('mean_ms', 0):.2f}ms")
            
            if "throughput" in metrics:
                throughput = metrics["throughput"]
                print(f"Current RPS: {throughput.get('current_rps', 0)}")
        
        print("\n🎯 Final Timer Statistics:")
        print("=" * 35)
        
        timer_stats = service.get_timer_stats()
        for timer_info in timer_stats["timers"]:
            print(f"\n📋 {timer_info['method_name']}:")
            print(f"  • Executions: {timer_info['execution_count']}")
            print(f"  • Errors: {timer_info['error_count']}")
            print(f"  • Error Rate: {timer_info['error_rate']:.1f}%")
            print(f"  • Avg Duration: {timer_info['average_execution_time']:.3f}s")
            print(f"  • Total Runtime: {timer_info['total_execution_time']:.2f}s")
        
        print("\n✨ Timer Metrics Integration Features:")
        print("  • Automatic execution counting")  
        print("  • Duration tracking in milliseconds")
        print("  • Error rate monitoring")
        print("  • Zero-overhead when metrics disabled")
        print("  • Integrates with PerformanceMetrics system")
        
    except KeyboardInterrupt:
        print("\n⏹️  Demo interrupted")
    finally:
        await service.stop()
        print("✅ Service stopped")


if __name__ == "__main__":
    asyncio.run(run_metrics_demo())