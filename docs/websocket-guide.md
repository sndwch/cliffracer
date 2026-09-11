# WebSocket Real-time Communication Guide

This guide covers how to build real-time applications using Cliffracer's WebSocket integration.

## Overview

Cliffracer provides WebSocket support for bidirectional real-time communication, perfect for chat applications, live updates, notifications, and collaborative features.

## Basic WebSocket Service

### Declaring the extension

WebSockets come from `cliffracer-http`, the same distribution as the REST
routes and the same `HttpExtension` attribute: one extension, one app, one
port.

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer_http import HttpExtension

class NotificationService(CliffracerService):
    http = HttpExtension(port=8081)

    def __init__(self):
        config = ServiceConfig(
            name="notification_service",
            nats_url="nats://localhost:4222"
        )
        super().__init__(config)

    @http.websocket("/ws/notifications")
    async def notifications(self, websocket):
        """One connected client, for as long as it stays connected."""
        await websocket.accept()
        await websocket.send_json({
            "type": "connection",
            "message": "Connected to notification service"
        })
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "subscribe":
                await websocket.send_json({
                    "type": "subscribed",
                    "channel": data.get("channel"),
                })
```

**The handler owns the whole connection**, from `accept()` to disconnect.
Three things follow from that:

- **Connect, message and disconnect are positions in your handler.** Connect
  is the code before your loop, message is the loop, disconnect is what happens
  when it ends.
- **The extension tracks clients for you.** It adds the socket to
  `self.http.active_connections` before calling you and discards it in a
  `finally`, so a handler that raises still leaves the set clean.
- `WebSocketDisconnect` is caught for you. Leaving the loop by disconnecting is
  the normal exit, not an error to handle.

The handler is **bound before the framework calls it**, so it takes `self` and
the socket in that order.

### Broadcasting

```python
await self.http.broadcast_to_websockets({"type": "alert", "body": "..."})
```

Sends to every connected client, serialising once. A socket that fails to send
is dropped from `active_connections` rather than stopping the fan-out — one
dead client cannot silence the rest.

`/health` and `/info` report the extension's view under its attribute name:
`{"http": {"websockets": {"active_connections": 2, "registered_handlers": 1}}}`
and the list of registered paths respectively.

## WebSocket Handlers

### Rooms

Anything beyond "all connected clients" is yours to keep, because only you know
what the grouping means. A room map lives on the service:

```python
from datetime import UTC, datetime

from fastapi import WebSocket, WebSocketDisconnect

from cliffracer import CliffracerService, ServiceConfig
from cliffracer_http import HttpExtension

class ChatService(CliffracerService):
    http = HttpExtension(port=8081)

    def __init__(self):
        super().__init__(ServiceConfig(name="chat_service"))
        self.rooms: dict[str, set[WebSocket]] = {}

    @http.websocket("/chat/{room_id}")
    async def chat_handler(self, websocket: WebSocket, room_id: str):
        """WebSocket endpoint for chat rooms"""
        await websocket.accept()
        self.rooms.setdefault(room_id, set()).add(websocket)
        try:
            await self.broadcast_to_room(room_id, {
                "type": "user_joined",
                "message": f"User joined room {room_id}",
            }, exclude=websocket)

            while True:
                data = await websocket.receive_json()
                if data["type"] == "message":
                    await self.broadcast_to_room(room_id, {
                        "type": "message",
                        "user": data.get("user", "Anonymous"),
                        "message": data["message"],
                        "timestamp": datetime.now(UTC).isoformat(),
                    })
        except WebSocketDisconnect:
            raise
        finally:
            # A `finally`, not an except: a handler that raises for any other
            # reason must not leave a dead socket in the room. The extension
            # does the same for active_connections, and for the same reason.
            self.rooms[room_id].discard(websocket)
            await self.broadcast_to_room(room_id, {
                "type": "user_left",
                "message": "User left the room",
            })

    async def broadcast_to_room(self, room_id: str, message: dict, exclude=None):
        """Broadcast to everyone in one room"""
        dead = set()
        for client in self.rooms.get(room_id, set()):
            if client is exclude:
                continue
            try:
                await client.send_json(message)
            except Exception:
                dead.add(client)
        self.rooms[room_id] -= dead
```

Two things to copy from this: cleanup goes in a `finally` rather than only in
the disconnect branch, so a handler that raises for any other reason still
leaves the room; and a failed send collects the socket rather than `except:
pass`, which would swallow `KeyboardInterrupt` and `CancelledError` along with
the broken pipe.

## NATS to WebSocket Bridge

Bridge NATS events to WebSocket clients:

```python
from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer_http import HttpExtension

class LiveUpdateService(CliffracerService):
    http = HttpExtension(port=8081)

    def __init__(self):
        super().__init__(ServiceConfig(name="live_update"))
        self.subscribers = {}  # topic -> set of websockets

    @listener("order.*", fanout=True)
    async def on_order_event(self, subject: str, order_id: str = ""):
        """Forward order events to WebSocket clients"""
        await self.broadcast_to_subscribers("order.update", {
            "type": "order_update",
            "order_id": order_id
        })

    @listener("payment.completed", fanout=True)
    async def on_payment_completed(self, subject: str, order_id: str = "", amount: float = 0.0):
        """Forward payment events"""
        await self.broadcast_to_subscribers("payments", {
            "type": "payment_completed",
            "order_id": order_id,
            "amount": amount
        })

    async def broadcast_to_subscribers(self, topic: str, message: dict):
        """Send message to all subscribers of a topic"""
        if topic in self.subscribers:
            disconnected = []
            for ws in self.subscribers[topic]:
                try:
                    await ws.send_json(message)
                except Exception:
                    disconnected.append(ws)

            # Clean up disconnected clients
            for ws in disconnected:
                self.subscribers[topic].remove(ws)
```

## Authentication with WebSockets

`AuthExtension` guards NATS dispatch. **A WebSocket connection is
authenticated in the handler**, by you, before you accept anything the client
says.

A browser sends the token as a query parameter, so the first thing the handler
does is validate it:

```python
from fastapi import Query, WebSocket, WebSocketDisconnect

from cliffracer import CliffracerService, ServiceConfig
from cliffracer_auth import AuthConfig, SimpleAuthService
from cliffracer_http import HttpExtension

auth = SimpleAuthService(AuthConfig(secret_key="a-secret-key-of-at-least-32-characters"))

class SecureWebSocketService(CliffracerService):
    http = HttpExtension(port=8081)

    def __init__(self):
        super().__init__(ServiceConfig(name="secure_ws"))
        self.users: dict[WebSocket, str] = {}

    @http.websocket("/ws")
    async def websocket_endpoint(self, websocket: WebSocket, token: str = Query(...)):
        context = auth.validate_token(token)
        if not context:
            # Close BEFORE accept: an unauthenticated client must not reach a
            # state where it can send anything.
            await websocket.close(code=1008, reason="unauthenticated")
            return

        await websocket.accept()
        self.users[websocket] = context.user.username
        try:
            while True:
                data = await websocket.receive_json()
                await websocket.send_json({"echo": data, "as": self.users[websocket]})
        except WebSocketDisconnect:
            raise
        finally:
            self.users.pop(websocket, None)
```

Two things this example is careful about:

- **Close before accept.** Accepting and then closing gives the client a window
  in which it is connected and unauthenticated.
- **Remove the identity in a `finally`.** `self.users` is keyed by socket
  object; an entry left behind outlives the connection and the next handler to
  read it gets a username for a socket that is gone.

Check roles explicitly here — `context.user.roles` is the set to test.
`@requires_roles` reads the auth contextvar that `AuthExtension.worker_setup`
sets on a NATS dispatch, and a WebSocket connection is not one.

## Broadcasting Patterns

### Broadcast to All Clients

```python
from datetime import UTC, datetime

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer_http import HttpExtension

class BroadcastService(CliffracerService):
    http = HttpExtension(port=8081)

    @rpc
    async def send_announcement(self, text: str) -> None:
        """An RPC that reaches every connected WebSocket client"""
        await self.http.broadcast_to_websockets({
            "type": "announcement",
            "text": text,
            "timestamp": datetime.now(UTC).isoformat(),
        })
```

### Selective Broadcasting

```python
class TargetedBroadcastService(CliffracerService):
    http = HttpExtension(port=8081)

    def __init__(self):
        super().__init__(config)
        self.user_connections = {}  # user_id -> websocket
        self.subscriptions = {}  # topic -> set of user_ids
    
    async def send_to_user(self, user_id: str, message: dict):
        """Send message to specific user"""
        if user_id in self.user_connections:
            try:
                await self.user_connections[user_id].send_json(message)
            except:
                del self.user_connections[user_id]
    
    async def send_to_topic_subscribers(self, topic: str, message: dict):
        """Send to all subscribers of a topic"""
        if topic in self.subscriptions:
            for user_id in self.subscriptions[topic]:
                await self.send_to_user(user_id, message)
    
    async def send_to_users_with_role(self, role: str, message: dict):
        """Send to all users with specific role"""
        for websocket, user in self.authenticated_clients.items():
            if role in user.roles:
                try:
                    await websocket.send_json(message)
                except:
                    pass
```

## Client-Side Examples

### JavaScript Client

```javascript
// Connect to WebSocket
const ws = new WebSocket('ws://localhost:8081/ws?token=' + authToken);

// Connection opened
ws.onopen = (event) => {
    console.log('Connected to WebSocket');
    
    // Subscribe to updates
    ws.send(JSON.stringify({
        type: 'subscribe',
        topics: ['orders', 'notifications']
    }));
};

// Handle messages
ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    
    switch(data.type) {
        case 'order_update':
            updateOrderDisplay(data.data);
            break;
        case 'notification':
            showNotification(data.message);
            break;
    }
};

// Send message
function sendMessage(message) {
    ws.send(JSON.stringify({
        type: 'message',
        content: message
    }));
}

// Handle errors
ws.onerror = (error) => {
    console.error('WebSocket error:', error);
};

// Handle close
ws.onclose = (event) => {
    console.log('WebSocket closed:', event.code, event.reason);
    // Implement reconnection logic
};
```

### Python Client

```python
import asyncio
import websockets
import json

async def websocket_client():
    uri = "ws://localhost:8081/ws?token=your-token"
    
    async with websockets.connect(uri) as websocket:
        # Send initial message
        await websocket.send(json.dumps({
            "type": "subscribe",
            "topics": ["orders", "payments"]
        }))
        
        # Listen for messages
        async for message in websocket:
            data = json.loads(message)
            print(f"Received: {data}")
            
            # Handle different message types
            if data["type"] == "order_update":
                print(f"Order updated: {data['data']}")

# Run client
asyncio.run(websocket_client())
```

## Advanced Patterns

### Heartbeat/Ping-Pong

Keep connections alive and detect disconnections:

```python
class HeartbeatService(CliffracerService):
    http = HttpExtension(port=8081)

    def __init__(self):
        super().__init__(config)
        self.client_last_seen = {}
    
    async def start_heartbeat(self):
        """Send periodic heartbeat to all clients"""
        while True:
            await asyncio.sleep(30)  # Every 30 seconds
            
            disconnected = []
            for ws in list(self.http.active_connections):
                try:
                    await ws.send_json({"type": "ping"})
                except:
                    disconnected.append(ws)
            
            # Clean up
            for ws in disconnected:
                self.http.active_connections.discard(ws)
    
    async def on_websocket_message(self, websocket, data):
        if data.get("type") == "pong":
            self.client_last_seen[websocket] = datetime.now()
```

### Rate Limiting

Prevent WebSocket abuse:

```python
from collections import defaultdict
from datetime import datetime, timedelta

class RateLimitedWebSocketService(CliffracerService):
    http = HttpExtension(port=8081)

    def __init__(self):
        super().__init__(config)
        self.message_counts = defaultdict(list)
        self.max_messages_per_minute = 60
    
    async def check_rate_limit(self, websocket) -> bool:
        """Check if client exceeded rate limit"""
        now = datetime.now()
        minute_ago = now - timedelta(minutes=1)
        
        # Clean old entries
        self.message_counts[websocket] = [
            timestamp for timestamp in self.message_counts[websocket]
            if timestamp > minute_ago
        ]
        
        # Check limit
        if len(self.message_counts[websocket]) >= self.max_messages_per_minute:
            return False
        
        # Record message
        self.message_counts[websocket].append(now)
        return True
    
    async def on_websocket_message(self, websocket, data):
        if not await self.check_rate_limit(websocket):
            await websocket.send_json({
                "type": "error",
                "message": "Rate limit exceeded"
            })
            return
        
        # Process message normally
        await self.handle_message(websocket, data)
```

## Testing WebSockets

### Unit Testing

```python
import pytest
from fastapi.testclient import TestClient

@pytest.mark.asyncio
async def test_websocket_connection():
    service = ChatService()
    client = TestClient(service.http.app)
    
    with client.websocket_connect("/chat/room1") as websocket:
        # Test connection
        data = websocket.receive_json()
        assert data["type"] == "connection"
        
        # Test sending message
        websocket.send_json({
            "type": "message",
            "message": "Hello, world!"
        })
        
        # Test receiving broadcast
        response = websocket.receive_json()
        assert response["type"] == "message"
        assert response["message"] == "Hello, world!"
```

### Load Testing

```python
import asyncio
import websockets

async def load_test_client(client_id: int):
    """Simulate a single client"""
    uri = f"ws://localhost:8081/ws?token=test-token-{client_id}"
    
    async with websockets.connect(uri) as websocket:
        # Send messages
        for i in range(100):
            await websocket.send(json.dumps({
                "type": "message",
                "content": f"Message {i} from client {client_id}"
            }))
            await asyncio.sleep(0.1)

async def run_load_test(num_clients: int):
    """Run load test with multiple clients"""
    tasks = [load_test_client(i) for i in range(num_clients)]
    await asyncio.gather(*tasks)

# Run with 100 concurrent clients
asyncio.run(run_load_test(100))
```

## Best Practices

### 1. Connection Management
- Track active connections
- Implement heartbeat/ping-pong
- Clean up disconnected clients
- Set appropriate timeouts

### 2. Error Handling
- Gracefully handle disconnections
- Validate all incoming messages
- Send clear error messages
- Log errors for debugging

### 3. Security
- Always authenticate connections
- Validate message content
- Implement rate limiting
- Use WSS in production

### 4. Performance
- Batch messages when possible
- Use message queues for high volume
- Consider horizontal scaling
- Monitor connection counts

## Troubleshooting

### Connection Drops
```javascript
// Implement automatic reconnection on the client
let reconnectInterval = 1000;

function connect() {
    const ws = new WebSocket(wsUrl);
    
    ws.onclose = () => {
        setTimeout(connect, reconnectInterval);
        reconnectInterval = Math.min(reconnectInterval * 2, 30000);
    };
    
    ws.onopen = () => {
        reconnectInterval = 1000;  // Reset on successful connection
    };
}
```

### Memory Leaks
```python
# Always clean up references
async def on_websocket_disconnect(self, websocket):
    # Remove from all tracking structures
    self.http.active_connections.discard(websocket)
    self.authenticated_clients.pop(websocket, None)
    
    # Clean up subscriptions
    for topic, subscribers in self.subscriptions.items():
        subscribers.discard(websocket)
```

### CORS Issues
```python
# Configure CORS for WebSocket connections
from fastapi.middleware.cors import CORSMiddleware

# In on_startup: the app does not exist until the extension's setup() runs.
self.http.app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```