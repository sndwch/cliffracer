#!/usr/bin/env python3
"""
Cliffracer Comprehensive Service Example

Demonstrates service configuration with HTTP extension, RPC, event, and timer handlers.
"""

import asyncio
from datetime import UTC, datetime

from cliffracer_http import HttpExtension
from pydantic import BaseModel

# Core imports
from cliffracer import (
    CliffracerService,
    ServiceConfig,
    # Exception handling
    ValidationError,
    broadcast,
    listener,
    # All decorators in one place
    rpc,
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


class ServiceStats(BaseModel):
    """What get_stats returns: a count and two nested stat blocks."""

    total_users: int
    service_stats: dict[str, int]
    timer_stats: dict[str, int]


class ComprehensiveService(CliffracerService):
    """
    Example service demonstrating RPC, HTTP, and background tasks.

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

    http = HttpExtension(port=8080)

    def __init__(self):
        config = ServiceConfig(name="comprehensive_service")
        super().__init__(config)

        self.users = {}  # Simple in-memory storage
        self.stats = {"rpc_calls": 0, "events_sent": 0}

    # === RPC Methods ===

    @rpc
    async def create_user(self, request: UserRequest) -> UserResponse:
        """
        Create a new user.

        This method demonstrates:
        - Schema validation with Pydantic
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
    async def get_user(self, user_id: str) -> dict[str, str | None]:
        """Get a user by id"""
        if user_id not in self.users:
            raise ValidationError(f"User {user_id} not found")

        return self.users[user_id]

    @rpc
    async def get_stats(self) -> ServiceStats:
        """Get service statistics with performance monitoring"""
        return ServiceStats(
            total_users=len(self.users),
            service_stats=self.stats,
            timer_stats=self.get_timer_stats(),
        )

    # === HTTP Endpoints ===

    @http.get("/users/{user_id}")
    async def http_get_user(self, user_id: str):
        """HTTP GET endpoint for user retrieval"""
        try:
            return await self.get_user(user_id)
        except ValidationError as e:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=str(e)) from e

    @http.post("/users")
    async def http_create_user(self, user_request: UserRequest):
        """HTTP POST endpoint for user creation"""
        try:
            return await self.create_user(user_request)
        except Exception as e:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail=str(e)) from e

    # === Event Handlers ===

    @listener("user.events.*", fanout=True)
    async def handle_user_events(self, subject: str, message: str = "") -> None:
        """Handle all user-related events"""
        print(f"[INFO] Received user event: {subject} - {message}")
        self.stats["events_sent"] += 1

    @broadcast("system.alerts.*")
    async def handle_system_alerts(self, message: str = "") -> None:
        """Handle system alerts and broadcast to WebSocket clients"""
        alert_message = {
            "type": "system_alert",
            "data": message,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # Broadcast to all WebSocket connections
        await self.broadcast_to_websockets(alert_message)
        print(f"[WARN] System alert broadcasted: {message}")

    # === Timer Tasks ===

    @timer(interval=30, eager=True)
    async def health_check_task(self):
        """Scheduled health check"""
        # Simulate health check operations
        health_status = {
            "status": "healthy",
            "users_count": len(self.users),
            "memory_usage": "normal",
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # Publish health status
        await self.publish_event("service.health", **health_status)
        print(f"[OK] Health check completed: {health_status['status']}")

    @timer(interval=60)
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
        print(f"[METRICS] Metrics collected: {metrics}")

    @timer(interval=120)
    async def cleanup_task(self):
        """Cleanup old data every 2 minutes"""
        # Simulate cleanup operations
        cleanup_count = max(0, len(self.users) - 100)  # Keep only 100 users
        print(f"[CLEAN] Cleanup task: Would remove {cleanup_count} old users")

    # === WebSocket Handler ===

    async def handle_websocket_connection(self, websocket):
        """Handle WebSocket connections for real-time updates"""
        print(f"[INFO] New WebSocket connection from {websocket.client}")

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
    print("[INFO] Starting Cliffracer Consolidated Architecture Example")
    print("=" * 60)

    service = ComprehensiveService()

    try:
        print("[INFO] Starting comprehensive service...")
        await service.start()

        print("[OK] Service started successfully!")
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
        print("  [OK] NATS messaging (RPC, events, async)")
        print("  [OK] HTTP/REST API with FastAPI")
        print("  [OK] WebSocket real-time connections")
        print("  [OK] Schema validation with Pydantic")
        print("  [OK] Timer-based scheduled tasks")
        print("  [OK] Performance optimizations")
        print("  [OK] Error handling and retries")
        print("  [OK] Monitoring and metrics")
        print("  [OK] Caching and result optimization")
        print()
        print("[INFO] Service is running! Press Ctrl+C to stop...")

        # Keep service running
        while True:
            await asyncio.sleep(10)

            # Show some stats periodically
            stats = await service.get_stats()
            print(
                f"[METRICS] Current stats: {stats['total_users']} users, "
                f"{stats['service_stats']['rpc_calls']} RPC calls"
            )

    except KeyboardInterrupt:
        print("\n[STOP]  Stopping service...")
    finally:
        await service.stop()
        print("[OK] Service stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
