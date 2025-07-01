"""
WebSocket Test Client
====================

Test client for the WebSocket-enabled notification service.
Demonstrates both sending notifications via RPC and receiving them via WebSocket.
"""

import asyncio
import json
from datetime import datetime

import websockets
from nats.aio.client import Client as NATS


async def websocket_listener():
    """Connect to WebSocket and listen for notifications"""
    uri = "ws://localhost:8002/ws/notifications"

    print(f"🔌 Connecting to WebSocket at {uri}...")

    try:
        async with websockets.connect(uri) as websocket:
            print("✅ Connected to WebSocket!")

            # Send initial ping
            await websocket.send(json.dumps({"type": "ping"}))

            # Listen for messages
            async for message in websocket:
                data = json.loads(message)
                timestamp = datetime.now().strftime("%H:%M:%S")

                if data.get("type") == "connection":
                    print(f"[{timestamp}] 🤝 {data['message']}")
                elif data.get("type") == "pong":
                    print(f"[{timestamp}] 🏓 Pong received")
                elif data.get("type") == "notification":
                    notif = data["data"]
                    print(f"[{timestamp}] 📬 Notification: {notif['title']}")
                    print(f"    User: {notif['user_id']}")
                    print(f"    Message: {notif['message']}")
                    print(f"    Type: {notif['notification_type']}")
                elif data.get("type") == "broadcast":
                    # BroadcastMessages are automatically relayed
                    print(f"[{timestamp}] 📡 Broadcast: {data['data']['source_service']}")
                    print(f"    Data: {json.dumps(data['data'], indent=2)}")
                else:
                    print(f"[{timestamp}] 📨 {json.dumps(data, indent=2)}")

    except Exception as e:
        print(f"❌ WebSocket error: {e}")


async def send_test_notifications():
    """Send test notifications via NATS RPC"""
    nc = NATS()

    print("🔗 Connecting to NATS...")
    await nc.connect("nats://localhost:4222")
    print("✅ Connected to NATS!")

    # Wait a bit for WebSocket to connect
    await asyncio.sleep(2)

    # Send various test notifications
    notifications = [
        {
            "user_id": "user_123",
            "title": "Welcome!",
            "message": "Welcome to the real-time notification system",
            "notification_type": "info",
        },
        {
            "user_id": "user_456",
            "title": "New Order",
            "message": "Order #12345 has been placed",
            "notification_type": "success",
        },
        {
            "user_id": "user_789",
            "title": "Payment Failed",
            "message": "Payment for order #67890 failed",
            "notification_type": "error",
        },
        {
            "user_id": "user_123",
            "title": "Shipping Update",
            "message": "Your order has been shipped!",
            "notification_type": "info",
        },
    ]

    for i, notif in enumerate(notifications, 1):
        print(f"\n📤 Sending notification {i}/{len(notifications)}...")

        # Send RPC request
        request_data = json.dumps(notif).encode()

        try:
            response = await nc.request(
                "notification_service.rpc.send_notification",
                request_data,
                timeout=5.0,
            )

            result = json.loads(response.data.decode())
            print(f"✅ Sent! Notification ID: {result['result']['notification_id']}")
            print(f"   WebSocket clients notified: {result['result']['websocket_clients_notified']}")

        except Exception as e:
            print(f"❌ Error sending notification: {e}")

        # Wait between notifications
        await asyncio.sleep(2)

    await nc.close()


async def main():
    """Run WebSocket listener and notification sender concurrently"""
    print("🚀 WebSocket Notification Test Client")
    print("=" * 50)
    print("This client will:")
    print("1. Connect to the WebSocket endpoint")
    print("2. Send test notifications via NATS RPC")
    print("3. Display received WebSocket notifications")
    print("=" * 50)
    print()

    # Run WebSocket listener and notification sender concurrently
    await asyncio.gather(
        websocket_listener(),
        send_test_notifications(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Test client stopped")
