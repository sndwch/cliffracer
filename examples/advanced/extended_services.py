"""
Example services demonstrating extended NATS framework features
"""

import asyncio
import logging
from datetime import datetime

from pydantic import BaseModel, Field

from cliffracer import (
    BroadcastMessage,
    HTTPNATSService,
    RPCRequest,
    RPCResponse,
    ServiceConfig,
    ServiceOrchestrator,
    ValidatedNATSService,
    broadcast,
    listener,
    rpc,
    validated_rpc,
)
from cliffracer.core.base_service import event_handler

logger = logging.getLogger(__name__)


# Define message schemas
class CreateUserRequest(RPCRequest):
    """Request to create a new user"""

    username: str = Field(..., min_length=3, max_length=50)
    email: str = Field(..., pattern=r"^[\w\.-]+@[\w\.-]+\.\w+$")
    full_name: str


class CreateUserResponse(RPCResponse):
    """Response from user creation"""

    user_id: str
    created_at: datetime


class UserCreatedBroadcast(BroadcastMessage):
    """Broadcast when a user is created"""

    user_id: str
    username: str
    email: str


class GetUserRequest(RPCRequest):
    """Request to get user details"""

    user_id: str


class UserResponse(RPCResponse):
    """User details response"""

    user_id: str
    username: str
    email: str
    full_name: str
    created_at: datetime
    last_active: datetime | None = None


class UserActivityBroadcast(BroadcastMessage):
    """Broadcast user activity events"""

    user_id: str
    action: str
    details: dict = {}


class NotificationRequest(BaseModel):
    """HTTP request to send notification"""

    user_id: str
    message: str
    priority: str = "normal"


# Services
class UserService(HTTPNATSService):
    """User management service with HTTP API"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config, port=8001)
        self.users = {}

        # Add HTTP routes
        @self.post("/api/users", response_model=CreateUserResponse)
        async def create_user_http(request: CreateUserRequest):
            """HTTP endpoint to create user"""
            return await self.create_user(request)

        @self.get("/api/users/{user_id}", response_model=UserResponse)
        async def get_user_http(user_id: str):
            """HTTP endpoint to get user"""
            request = GetUserRequest(user_id=user_id)
            return await self.get_user(request)

    @validated_rpc(CreateUserRequest, CreateUserResponse)
    async def create_user(self, request: CreateUserRequest) -> CreateUserResponse:
        """Create a new user with validation"""
        user_id = f"user_{len(self.users) + 1}"

        self.users[user_id] = {
            "user_id": user_id,
            "username": request.username,
            "email": request.email,
            "full_name": request.full_name,
            "created_at": datetime.utcnow(),
            "last_active": None,
        }

        # Broadcast user created event
        await self.broadcast_user_created(user_id, request.username, request.email)

        return CreateUserResponse(user_id=user_id, created_at=self.users[user_id]["created_at"])

    @validated_rpc(GetUserRequest, UserResponse)
    async def get_user(self, request: GetUserRequest) -> UserResponse:
        """Get user details"""
        user = self.users.get(request.user_id)
        if not user:
            raise ValueError(f"User {request.user_id} not found")

        return UserResponse(**user)

    @broadcast(UserCreatedBroadcast)
    async def broadcast_user_created(self, user_id: str, username: str, email: str):
        """Broadcast that a user was created"""
        return UserCreatedBroadcast(
            user_id=user_id, username=username, email=email, source_service=self.config.name
        )

    @event_handler("activity.*")
    async def track_user_activity(self, subject: str, user_id: str, **kwargs):
        """Track user activity"""
        if user_id in self.users:
            self.users[user_id]["last_active"] = datetime.utcnow()

        # Broadcast activity
        action = subject.split(".")[-1]
        await self.broadcast_message(
            UserActivityBroadcast(
                user_id=user_id, action=action, details=kwargs, source_service=self.config.name
            )
        )


class NotificationService(ValidatedNATSService):
    """Notification service with WebSocket support"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config, port=8002)
        self.notifications = []

        @self.post("/api/notify")
        async def send_notification_http(request: NotificationRequest):
            """HTTP endpoint to send notification"""
            await self.send_notification(
                user_id=request.user_id, message=request.message, priority=request.priority
            )
            return {"status": "sent"}

    @listener(UserCreatedBroadcast)
    async def on_user_created(self, message: UserCreatedBroadcast):
        """React to new user creation"""
        notification = {
            "type": "user_created",
            "user_id": message.user_id,
            "username": message.username,
            "message": f"Welcome {message.username}!",
            "timestamp": datetime.utcnow().isoformat(),
        }

        self.notifications.append(notification)

        # Send welcome email (simulated)
        await self.send_notification(
            user_id=message.user_id,
            message=f"Welcome to our service, {message.username}!",
            priority="high",
        )

        # Broadcast to WebSocket clients
        await self.broadcast_to_websockets(notification)

    @listener(UserActivityBroadcast)
    async def on_user_activity(self, message: UserActivityBroadcast):
        """React to user activity"""
        activity = {
            "type": "user_activity",
            "user_id": message.user_id,
            "action": message.action,
            "details": message.details,
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Broadcast to WebSocket clients
        await self.broadcast_to_websockets(activity)

    @rpc
    async def send_notification(self, user_id: str, message: str, priority: str = "normal"):
        """Send a notification"""
        notification = {
            "user_id": user_id,
            "message": message,
            "priority": priority,
            "sent_at": datetime.utcnow().isoformat(),
        }

        self.notifications.append(notification)

        # In real service, would send email/SMS/push
        print(f"[{priority.upper()}] Notification to {user_id}: {message}")

        # Broadcast to WebSocket clients
        await self.broadcast_to_websockets({"type": "notification_sent", **notification})

        return notification

    # @websocket_handler("/ws")  # WebSocket support not yet implemented
    async def handle_notifications_ws(self, websocket):
        """WebSocket handler for real-time notifications"""
        # Send initial connection message
        await websocket.send_json(
            {
                "type": "connected",
                "message": "Connected to notification service",
                "timestamp": datetime.utcnow().isoformat(),
            }
        )

        # Keep connection alive and handle messages
        try:
            while True:
                data = await websocket.receive_json()

                if data.get("type") == "subscribe":
                    user_id = data.get("user_id")
                    await websocket.send_json(
                        {
                            "type": "subscribed",
                            "user_id": user_id,
                            "message": f"Subscribed to notifications for {user_id}",
                        }
                    )
                elif data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})

        except Exception as e:
            logger.error(f"WebSocket error: {e}")


class AnalyticsService(ValidatedNATSService):
    """Analytics service that listens to all events"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.event_counts = {}
        self.user_stats = {}

    @listener(UserCreatedBroadcast)
    async def track_user_creation(self, message: UserCreatedBroadcast):
        """Track user creation stats"""
        self.event_counts["user_created"] = self.event_counts.get("user_created", 0) + 1
        self.user_stats[message.user_id] = {
            "created_at": message.timestamp,
            "username": message.username,
        }
        print(
            f"[Analytics] New user tracked: {message.username} (Total: {self.event_counts['user_created']})"
        )

    @listener(UserActivityBroadcast)
    async def track_user_activity(self, message: UserActivityBroadcast):
        """Track user activity stats"""
        action_key = f"activity_{message.action}"
        self.event_counts[action_key] = self.event_counts.get(action_key, 0) + 1

        if message.user_id in self.user_stats:
            self.user_stats[message.user_id]["last_activity"] = message.timestamp
            self.user_stats[message.user_id]["activity_count"] = (
                self.user_stats[message.user_id].get("activity_count", 0) + 1
            )

    @rpc
    async def get_stats(self):
        """Get analytics statistics"""
        return {
            "event_counts": self.event_counts,
            "total_users": len(self.user_stats),
            "timestamp": datetime.utcnow().isoformat(),
        }


async def test_extended_services():
    """Test the extended services"""

    # Create test client
    client_config = ServiceConfig(name="test_client")
    client = ValidatedNATSService(client_config)
    await client.connect()

    await asyncio.sleep(2)

    try:
        print("\n=== Creating User via RPC ===")
        # Call with raw data (will be validated)
        user_response = await client.call_rpc(
            "user_service",
            "create_user",
            username="john_doe",
            email="john@example.com",
            full_name="John Doe",
        )
        print(f"User created: {user_response}")

        print("\n=== Getting User Details ===")
        user_details = await client.call_rpc(
            "user_service", "get_user", user_id=user_response["user_id"]
        )
        print(f"User details: {user_details}")

        print("\n=== Simulating User Activity ===")
        await client.publish_event(
            "activity.login", user_id=user_response["user_id"], ip_address="192.168.1.1"
        )

        await asyncio.sleep(1)

        print("\n=== Getting Analytics ===")
        stats = await client.call_rpc("analytics_service", "get_stats")
        print(f"Analytics: {stats}")

        print("\n=== Testing Invalid Request ===")
        try:
            await client.call_rpc(
                "user_service",
                "create_user",
                username="ab",  # Too short
                email="invalid-email",  # Invalid format
                full_name="Test User",
            )
        except Exception as e:
            print(f"Validation error (expected): {e}")

    finally:
        await client.disconnect()


def run_extended_services():
    """Run all extended services"""
    # configure_logging()  # Function not implemented

    runner = ServiceOrchestrator()

    # Add services
    runner.add_service(UserService, ServiceConfig(name="user_service", auto_restart=True))

    runner.add_service(
        NotificationService, ServiceConfig(name="notification_service", auto_restart=True)
    )

    runner.add_service(AnalyticsService, ServiceConfig(name="analytics_service", auto_restart=True))

    runner.run_forever()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        asyncio.run(test_extended_services())
    else:
        run_extended_services()
