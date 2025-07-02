"""
WebSocket-Enabled Notification Service Example
=============================================

This example demonstrates a notification service that:
1. Accepts WebSocket connections for real-time notifications
2. Listens to NATS events and broadcasts them to WebSocket clients
3. Provides HTTP endpoints for sending notifications
4. Automatically relays all BroadcastMessages to connected WebSocket clients
"""

import asyncio
from datetime import UTC, datetime

from cliffracer import (
    BroadcastMessage,
    ServiceConfig,
    WebSocketNATSService,
    listener,
    validated_rpc,
    websocket_handler,
)


class NotificationMessage(BroadcastMessage):
    """Notification broadcast message"""

    notification_id: str
    user_id: str
    title: str
    message: str
    notification_type: str = "info"  # info, warning, error, success
    timestamp: datetime


class NotificationService(WebSocketNATSService):
    """WebSocket-enabled notification service"""

    def __init__(self):
        config = ServiceConfig(
            name="notification_service",
            description="Real-time notification delivery via WebSocket",
        )
        super().__init__(config, port=8002)
        self.notification_count = 0

    async def on_startup(self):
        """Service startup"""
        self.logger.info(
            "NotificationService starting up with WebSocket support",
            extra={"service": "notification_service", "port": self.port},
        )

    @websocket_handler("/ws/notifications")
    async def handle_notifications_ws(self, websocket):
        """
        WebSocket endpoint for real-time notifications

        Clients can connect to ws://localhost:8002/ws/notifications
        to receive real-time notifications.
        """
        # Send welcome message
        await websocket.send_json(
            {
                "type": "connection",
                "message": "Connected to notification service",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

        # Keep connection alive and handle incoming messages
        try:
            async for message in websocket.iter_json():
                # Echo back any received messages (for ping/pong)
                if message.get("type") == "ping":
                    await websocket.send_json(
                        {
                            "type": "pong",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                    )
        except Exception as e:
            self.logger.error(f"WebSocket error: {e}")

    @validated_rpc(dict, dict)
    async def send_notification(
        self, user_id: str, title: str, message: str, notification_type: str = "info"
    ) -> dict:
        """Send a notification via RPC"""
        self.notification_count += 1
        notification_id = f"notif_{self.notification_count}"

        # Create notification message
        notification = NotificationMessage(
            source_service=self.config.name,
            notification_id=notification_id,
            user_id=user_id,
            title=title,
            message=message,
            notification_type=notification_type,
            timestamp=datetime.now(UTC),
        )

        # Broadcast to all WebSocket clients
        await self.broadcast_to_websockets(
            {
                "type": "notification",
                "data": notification.model_dump(mode="json"),
            }
        )

        # Also broadcast via NATS for other services
        await self.broadcast_notification(notification)

        self.logger.info(
            "Notification sent",
            extra={
                "notification_id": notification_id,
                "user_id": user_id,
                "type": notification_type,
                "websocket_clients": len(self._active_connections),
            },
        )

        return {
            "notification_id": notification_id,
            "status": "sent",
            "websocket_clients_notified": len(self._active_connections),
        }

    @listener(NotificationMessage)
    async def on_notification_broadcast(self, message: NotificationMessage):
        """
        Listen for notifications from other services

        Note: WebSocketNATSService automatically relays BroadcastMessages
        to WebSocket clients, so this is just for logging.
        """
        self.logger.info(
            "Received notification broadcast",
            extra={
                "notification_id": message.notification_id,
                "source": message.source_service,
                "user_id": message.user_id,
            },
        )

    async def broadcast_notification(self, notification: NotificationMessage):
        """Broadcast notification to NATS"""
        await self.publish_event(
            "notifications.sent",
            **notification.model_dump(mode="json"),
        )


async def main():
    """Run the WebSocket-enabled notification service"""
    print("🚀 Starting WebSocket-Enabled Notification Service")
    print("=" * 50)
    print("WebSocket endpoint: ws://localhost:8002/ws/notifications")
    print("HTTP API docs: http://localhost:8002/docs")
    print("\nTest with: wscat -c ws://localhost:8002/ws/notifications")
    print("=" * 50)

    service = NotificationService()
    await service.start()

    # Keep service running
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\n🛑 Shutting down notification service...")
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
