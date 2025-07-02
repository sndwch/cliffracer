#!/usr/bin/env python3
"""
Comprehensive Timer Example for Cliffracer

This example demonstrates how to use the @timer decorator for scheduled tasks
in a microservices architecture, similar to Nameko's timer functionality.
"""

import asyncio
import time
from datetime import UTC, datetime

from cliffracer import NATSService, ServiceConfig, rpc, timer


class HealthMonitorService(NATSService):
    """
    A service that demonstrates various timer use cases:
    - Health checks
    - Metrics collection
    - Cache cleanup
    - Data synchronization
    """

    def __init__(self):
        config = ServiceConfig(name="health_monitor_service", nats_url="nats://localhost:4222")
        super().__init__(config)

        # Service state
        self.health_status = {"status": "unknown", "last_check": None}
        self.metrics_cache = {}
        self.cleanup_count = 0
        self.sync_count = 0
        self.database_connections = 0

        # Statistics
        self.health_check_count = 0
        self.metrics_collected = 0

    # Timer Examples

    @timer(interval=30)  # Every 30 seconds
    async def health_check(self):
        """
        Perform health checks every 30 seconds
        """
        self.health_check_count += 1

        try:
            # Simulate health check operations
            await self._check_database_connection()
            await self._check_external_services()

            self.health_status = {
                "status": "healthy",
                "last_check": datetime.now(UTC).isoformat(),
                "check_count": self.health_check_count,
                "database_connections": self.database_connections,
            }

            print(f"✅ Health check #{self.health_check_count} completed - Status: healthy")

        except Exception as e:
            self.health_status = {
                "status": "unhealthy",
                "last_check": datetime.now(UTC).isoformat(),
                "error": str(e),
                "check_count": self.health_check_count,
            }
            print(f"❌ Health check #{self.health_check_count} failed: {e}")

    @timer(interval=60, eager=True)  # Every minute, start immediately
    async def collect_metrics(self):
        """
        Collect and cache performance metrics every minute
        Start immediately when service starts (eager=True)
        """
        self.metrics_collected += 1

        # Simulate metrics collection
        current_metrics = {
            "timestamp": time.time(),
            "cpu_usage": 45.2,  # Simulated
            "memory_usage": 128.5,  # MB
            "active_connections": self.database_connections,
            "cache_size": len(self.metrics_cache),
            "collection_count": self.metrics_collected,
        }

        # Cache metrics with timestamp key
        timestamp_key = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        self.metrics_cache[timestamp_key] = current_metrics

        print(f"📊 Metrics collected #{self.metrics_collected}: {current_metrics}")

        # Publish metrics to other services
        await self.publish_event(
            "metrics.collected", service_name=self.config.name, metrics=current_metrics
        )

    @timer(interval=300)  # Every 5 minutes
    async def cleanup_old_data(self):
        """
        Clean up old cached data every 5 minutes
        """
        self.cleanup_count += 1

        current_time = time.time()
        old_keys = []

        # Find metrics older than 1 hour
        for key, metrics in self.metrics_cache.items():
            if current_time - metrics["timestamp"] > 3600:  # 1 hour
                old_keys.append(key)

        # Remove old metrics
        for key in old_keys:
            del self.metrics_cache[key]

        print(f"🧹 Cleanup #{self.cleanup_count}: Removed {len(old_keys)} old metric entries")

        # Log cleanup stats
        remaining_entries = len(self.metrics_cache)
        print(f"   Cache now contains {remaining_entries} metric entries")

    @timer(interval=120)  # Every 2 minutes
    async def sync_data(self):
        """
        Synchronize data with external systems every 2 minutes
        """
        self.sync_count += 1

        try:
            # Simulate data synchronization
            await asyncio.sleep(0.1)  # Simulate network call

            sync_result = {
                "sync_id": self.sync_count,
                "timestamp": datetime.now(UTC).isoformat(),
                "records_synced": 42,  # Simulated
                "status": "success",
            }

            print(f"🔄 Data sync #{self.sync_count} completed: {sync_result}")

            # Notify other services about sync completion
            await self.publish_event(
                "data.synced", service_name=self.config.name, result=sync_result
            )

        except Exception as e:
            print(f"❌ Data sync #{self.sync_count} failed: {e}")

    @timer(interval=10)  # Every 10 seconds (fast timer for demo)
    def connection_heartbeat(self):
        """
        Synchronous timer example - heartbeat for connections
        Note: This is a sync method (no async/await)
        """
        # Simulate connection management
        self.database_connections = max(
            0, self.database_connections + (-1 if time.time() % 20 < 10 else 1)
        )
        print(f"💓 Heartbeat - Active connections: {self.database_connections}")

    # Helper Methods

    async def _check_database_connection(self):
        """Simulate database health check"""
        await asyncio.sleep(0.05)  # Simulate DB query
        if time.time() % 30 < 25:  # Simulate occasional failure
            return True
        else:
            raise Exception("Database connection timeout")

    async def _check_external_services(self):
        """Simulate external service health checks"""
        await asyncio.sleep(0.02)  # Simulate API call
        return True

    # RPC Methods (for querying service state)

    @rpc
    async def get_health_status(self):
        """Get current health status"""
        return self.health_status

    @rpc
    async def get_metrics_summary(self):
        """Get metrics summary"""
        return {
            "total_metrics_collected": self.metrics_collected,
            "cached_metrics_count": len(self.metrics_cache),
            "cleanup_operations": self.cleanup_count,
            "sync_operations": self.sync_count,
            "health_checks": self.health_check_count,
        }

    @rpc
    async def get_timer_statistics(self):
        """Get timer execution statistics"""
        return self.get_timer_stats()

    @rpc
    async def get_recent_metrics(self, limit: int = 5):
        """Get most recent metrics"""
        sorted_metrics = sorted(
            self.metrics_cache.items(), key=lambda x: x[1]["timestamp"], reverse=True
        )
        return dict(sorted_metrics[:limit])


class TimerClientExample:
    """Example client to interact with the timer service"""

    def __init__(self):
        config = ServiceConfig(name="timer_client")
        self.service = NATSService(config)

    async def run_demo(self):
        """Run a demo of the timer service"""
        await self.service.start()

        try:
            print("🚀 Timer Service Demo Client")
            print("=" * 50)

            # Wait a bit for the service to run some timers
            print("⏱️  Waiting for timers to execute...")
            await asyncio.sleep(65)  # Wait over a minute to see multiple timer executions

            # Query service status
            print("\n📊 Querying service status...")

            health = await self.service.call_rpc("health_monitor_service", "get_health_status")
            print(f"Health Status: {health}")

            metrics = await self.service.call_rpc("health_monitor_service", "get_metrics_summary")
            print(f"Metrics Summary: {metrics}")

            timer_stats = await self.service.call_rpc(
                "health_monitor_service", "get_timer_statistics"
            )
            print(f"Timer Statistics: {timer_stats}")

            recent_metrics = await self.service.call_rpc(
                "health_monitor_service", "get_recent_metrics", limit=3
            )
            print(f"Recent Metrics: {recent_metrics}")

        finally:
            await self.service.stop()


async def main():
    """
    Main example runner
    """
    print("🎯 Cliffracer Timer Example")
    print("This example shows timers similar to Nameko's @timer decorator")
    print("=" * 60)

    # Create and start the service
    service = HealthMonitorService()

    try:
        print("🚀 Starting Health Monitor Service...")
        await service.start()

        print("✅ Service started! Timers are now running...")
        print("You should see timer executions below:")
        print()

        # Let the service run for a while to demonstrate timers
        await asyncio.sleep(90)  # Run for 90 seconds

        print("\n📈 Final Timer Statistics:")
        timer_stats = service.get_timer_stats()
        for timer_info in timer_stats["timers"]:
            print(f"  {timer_info['method_name']}: {timer_info['execution_count']} executions")

    except KeyboardInterrupt:
        print("\n⏹️  Stopping service...")
    finally:
        await service.stop()
        print("✅ Service stopped")


if __name__ == "__main__":
    # Run the example
    asyncio.run(main())
