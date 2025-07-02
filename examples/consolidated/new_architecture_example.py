#!/usr/bin/env python3
"""
Cliffracer Consolidated Architecture Example

This example demonstrates the new consolidated service architecture that
replaces the old BaseNATSService/ExtendedNATSService hierarchy with a
clean, mixin-based approach.
"""

import asyncio
from datetime import UTC, datetime

from pydantic import BaseModel

# Import from the new consolidated architecture
from cliffracer import (
    FullFeaturedService,  # All features enabled
    ServiceConfig,
    # Exception handling
    ValidationError,
    broadcast,
    cache_result,
    get,
    listener,
    monitor_performance,
    post,
    retry,
    robust_rpc,
    # All decorators in one place
    rpc,
    scheduled_task,
    timer,
)


# Pydantic schemas for validation
class UserRequest(BaseModel):
    username: str
    email: str
    full_name: str = ""


class UserResponse(BaseModel):
    user_id: str
    username: str
    status: str


class ComprehensiveService(FullFeaturedService):
    """
    Example service using the new consolidated architecture.

    This single service class provides:
    - NATS messaging (RPC, events, async calls)
    - HTTP/REST API endpoints
    - WebSocket real-time connections
    - Schema validation with Pydantic
    - Timer-based scheduled tasks
    - Performance optimizations
    - Error handling
    - Monitoring and metrics
    """

    def __init__(self):
        config = ServiceConfig(name="comprehensive_service", nats_url="nats://localhost:4222")
        super().__init__(
            config,
            host="0.0.0.0",
            port=8080,
            # Performance features
            enable_connection_pooling=True,
            enable_batch_processing=True,
            enable_metrics=True,
        )

        self.users = {}  # Simple in-memory storage
        self.stats = {"rpc_calls": 0, "events_sent": 0}

    # === RPC Methods ===

    @robust_rpc(schema=UserRequest, max_attempts=3, monitor=True)
    async def create_user(self, request: UserRequest) -> UserResponse:
        """
        Create a new user with validation, retry, and monitoring.

        This method demonstrates:
        - Schema validation with Pydantic
        - Automatic retry on failure
        - Performance monitoring
        - Error handling
        """
        user_id = f"user_{len(self.users) + 1}"

        # Simulate potential failure for demo
        if len(self.users) % 5 == 4:  # Fail every 5th user
            raise ValueError("Simulated database error")

        user_data = {
            "user_id": user_id,
            "username": request.username,
            "email": request.email,
            "full_name": request.full_name,
            "created_at": datetime.now(UTC).isoformat(),
        }

        self.users[user_id] = user_data
        self.stats["rpc_calls"] += 1

        # Publish user creation event
        await self.broadcast_message("user.created", user_id=user_id, username=request.username)

        return UserResponse(user_id=user_id, username=request.username, status="created")

    @rpc
    @cache_result(ttl_seconds=30)
    async def get_user(self, user_id: str) -> dict:
        """Get user with result caching"""
        if user_id not in self.users:
            raise ValidationError(f"User {user_id} not found")

        return self.users[user_id]

    @rpc
    @monitor_performance()
    async def get_stats(self) -> dict:
        """Get service statistics with performance monitoring"""
        return {
            "total_users": len(self.users),
            "service_stats": self.stats,
            "performance_metrics": self.get_performance_metrics(),
            "timer_stats": self.get_timer_stats(),
        }

    # === HTTP Endpoints ===

    @get("/users/{user_id}")
    async def http_get_user(self, user_id: str):
        """HTTP GET endpoint for user retrieval"""
        try:
            return await self.get_user(user_id)
        except ValidationError as e:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=str(e)) from e

    @post("/users")
    async def http_create_user(self, user_request: UserRequest):
        """HTTP POST endpoint for user creation"""
        try:
            return await self.create_user(user_request)
        except Exception as e:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail=str(e)) from e

    # === Event Handlers ===

    @listener("user.events.*")
    async def handle_user_events(self, subject: str, **data):
        """Handle all user-related events"""
        print(f"📨 Received user event: {subject} - {data}")
        self.stats["events_sent"] += 1

    @broadcast("system.alerts.*")
    async def handle_system_alerts(self, **data):
        """Handle system alerts and broadcast to WebSocket clients"""
        alert_message = {
            "type": "system_alert",
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # Broadcast to all WebSocket connections
        await self.broadcast_to_websockets(alert_message)
        print(f"🚨 System alert broadcasted: {data}")

    # === Timer Tasks ===

    @scheduled_task(interval=30, eager=True, monitor=True, max_attempts=2)
    async def health_check_task(self):
        """Scheduled health check with monitoring and retry"""
        # Simulate health check operations
        health_status = {
            "status": "healthy",
            "users_count": len(self.users),
            "memory_usage": "normal",
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # Publish health status
        await self.publish_event("service.health", **health_status)
        print(f"💚 Health check completed: {health_status['status']}")

    @timer(interval=60)
    @retry(max_attempts=3)
    async def metrics_collection(self):
        """Collect and publish metrics every minute"""
        metrics = {
            "service_name": self.config.name,
            "uptime_seconds": 300,  # Simulated
            "total_users": len(self.users),
            "rpc_calls": self.stats["rpc_calls"],
            "events_processed": self.stats["events_sent"],
            "timestamp": datetime.now(UTC).isoformat(),
        }

        await self.publish_event("metrics.collected", **metrics)
        print(f"📊 Metrics collected: {metrics}")

    @timer(interval=120)
    async def cleanup_task(self):
        """Cleanup old data every 2 minutes"""
        # Simulate cleanup operations
        cleanup_count = max(0, len(self.users) - 100)  # Keep only 100 users
        print(f"🧹 Cleanup task: Would remove {cleanup_count} old users")

    # === WebSocket Handler ===

    async def handle_websocket_connection(self, websocket):
        """Handle WebSocket connections for real-time updates"""
        print(f"🔌 New WebSocket connection from {websocket.client}")

        # Send welcome message
        welcome = {
            "type": "welcome",
            "message": "Connected to Comprehensive Service",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await websocket.send_json(welcome)

        try:
            while True:
                # Listen for client messages
                message = await websocket.receive_json()

                # Echo back with timestamp
                response = {
                    "type": "echo",
                    "original": message,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                await websocket.send_json(response)

        except Exception as e:
            print(f"WebSocket error: {e}")


async def main():
    """
    Run the comprehensive service example
    """
    print("🚀 Starting Cliffracer Consolidated Architecture Example")
    print("=" * 60)

    service = ComprehensiveService()

    try:
        print("🔧 Starting comprehensive service...")
        await service.start()

        print("✅ Service started successfully!")
        print()
        print("Available endpoints:")
        print("  • NATS RPC: comprehensive_service.rpc.create_user")
        print("  • NATS RPC: comprehensive_service.rpc.get_user")
        print("  • NATS RPC: comprehensive_service.rpc.get_stats")
        print("  • HTTP POST: http://localhost:8080/users")
        print("  • HTTP GET:  http://localhost:8080/users/{user_id}")
        print("  • WebSocket: ws://localhost:8080/ws")
        print("  • Health:    http://localhost:8080/health")
        print("  • Info:      http://localhost:8080/info")
        print()
        print("Features enabled:")
        print("  ✅ NATS messaging (RPC, events, async)")
        print("  ✅ HTTP/REST API with FastAPI")
        print("  ✅ WebSocket real-time connections")
        print("  ✅ Schema validation with Pydantic")
        print("  ✅ Timer-based scheduled tasks")
        print("  ✅ Performance optimizations")
        print("  ✅ Error handling and retries")
        print("  ✅ Monitoring and metrics")
        print("  ✅ Caching and result optimization")
        print()
        print("🎯 Service is running! Press Ctrl+C to stop...")

        # Keep service running
        while True:
            await asyncio.sleep(10)

            # Show some stats periodically
            stats = await service.get_stats()
            print(
                f"📊 Current stats: {stats['total_users']} users, "
                f"{stats['service_stats']['rpc_calls']} RPC calls"
            )

    except KeyboardInterrupt:
        print("\n⏹️  Stopping service...")
    finally:
        await service.stop()
        print("✅ Service stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
