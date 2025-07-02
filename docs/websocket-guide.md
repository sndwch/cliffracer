# WebSocket Real-time Communication Guide

This guide covers how to build real-time applications using Cliffracer's WebSocket integration.

## Overview

Cliffracer provides WebSocket support for bidirectional real-time communication, perfect for chat applications, live updates, notifications, and collaborative features.

## Basic WebSocket Service

### Using WebSocketMixin

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.mixins import WebSocketMixin

class NotificationService(CliffracerService, WebSocketMixin):
    def __init__(self):
        config = ServiceConfig(
            name="notification_service",
            nats_url="nats://localhost:4222"
        )
        super().__init__(config)
        self._websocket_port = 8081  # WebSocket port
        
        # Track connected clients
        self.clients = set()
    
    async def on_websocket_connect(self, websocket):
        """Called when a client connects"""
        self.clients.add(websocket)
        await websocket.send_json({
            "type": "connection",
            "message": "Connected to notification service"
        })
    
    async def on_websocket_disconnect(self, websocket):
        """Called when a client disconnects"""
        self.clients.remove(websocket)
    
    async def on_websocket_message(self, websocket, data):
        """Handle incoming WebSocket messages"""
        if data.get("type") == "subscribe":
            # Handle subscription logic
            await websocket.send_json({
                "type": "subscribed",
                "channel": data.get("channel")
            })
```

## WebSocket Handlers

### Message Handling

```python
from cliffracer.core.decorators import websocket_handler
from fastapi import WebSocket

class ChatService(CliffracerService, WebSocketMixin):
    def __init__(self):
        super().__init__(config)
        self._websocket_port = 8081
        self.rooms = {}  # room_id -> set of websockets
    
    @websocket_handler("/chat/{room_id}")
    async def chat_handler(self, websocket: WebSocket, room_id: str):
        """WebSocket endpoint for chat rooms"""
        await websocket.accept()
        
        # Add to room
        if room_id not in self.rooms:
            self.rooms[room_id] = set()
        self.rooms[room_id].add(websocket)
        
        try:
            # Notify others in room
            await self.broadcast_to_room(room_id, {
                "type": "user_joined",
                "message": f"User joined room {room_id}"
            }, exclude=websocket)
            
            # Handle messages
            while True:
                data = await websocket.receive_json()
                
                if data["type"] == "message":
                    # Broadcast to room
                    await self.broadcast_to_room(room_id, {
                        "type": "message",
                        "user": data.get("user", "Anonymous"),
                        "message": data["message"],
                        "timestamp": datetime.now().isoformat()
                    })
                
        except WebSocketDisconnect:
            # Remove from room
            self.rooms[room_id].remove(websocket)
            await self.broadcast_to_room(room_id, {
                "type": "user_left",
                "message": "User left the room"
            })
    
    async def broadcast_to_room(self, room_id: str, message: dict, exclude=None):
        """Broadcast message to all clients in a room"""
        if room_id in self.rooms:
            for client in self.rooms[room_id]:
                if client != exclude:
                    try:
                        await client.send_json(message)
                    except:
                        # Client disconnected
                        pass
```

## NATS to WebSocket Bridge

Bridge NATS events to WebSocket clients:

```python
class LiveUpdateService(CliffracerService, WebSocketMixin):
    def __init__(self):
        super().__init__(config)
        self._websocket_port = 8081
        self.subscribers = {}  # topic -> set of websockets
    
    @self.event("order.*")
    async def on_order_event(self, data: dict):
        """Forward order events to WebSocket clients"""
        topic = data.get("topic", "order.update")
        await self.broadcast_to_subscribers(topic, {
            "type": "order_update",
            "data": data
        })
    
    @self.event("payment.completed")
    async def on_payment_completed(self, data: dict):
        """Forward payment events"""
        await self.broadcast_to_subscribers("payments", {
            "type": "payment_completed",
            "order_id": data.get("order_id"),
            "amount": data.get("amount")
        })
    
    async def broadcast_to_subscribers(self, topic: str, message: dict):
        """Send message to all subscribers of a topic"""
        if topic in self.subscribers:
            disconnected = []
            for ws in self.subscribers[topic]:
                try:
                    await ws.send_json(message)
                except:
                    disconnected.append(ws)
            
            # Clean up disconnected clients
            for ws in disconnected:
                self.subscribers[topic].remove(ws)
```

## Authentication with WebSockets

Secure WebSocket connections:

```python
from cliffracer.auth.simple_auth import SimpleAuthService, AuthConfig
from fastapi import WebSocket, WebSocketDisconnect, Query
import jwt

class SecureWebSocketService(CliffracerService, WebSocketMixin):
    def __init__(self):
        super().__init__(config)
        self._websocket_port = 8081
        
        # Setup auth
        auth_config = AuthConfig(secret_key="your-secret-key")
        self.auth = SimpleAuthService(auth_config)
        
        self.authenticated_clients = {}  # websocket -> user
    
    @websocket_handler("/ws")
    async def websocket_endpoint(self, websocket: WebSocket, token: str = Query(...)):
        """Secure WebSocket endpoint requiring token"""
        # Validate token before accepting connection
        context = self.auth.validate_token(token)
        if not context:
            await websocket.close(code=1008, reason="Invalid token")
            return
        
        await websocket.accept()
        self.authenticated_clients[websocket] = context.user
        
        try:
            # Send user info
            await websocket.send_json({
                "type": "authenticated",
                "user": {
                    "id": context.user.id,
                    "username": context.user.username,
                    "roles": context.user.roles
                }
            })
            
            # Handle messages
            while True:
                data = await websocket.receive_json()
                # Process authenticated messages
                await self.handle_authenticated_message(websocket, data)
                
        except WebSocketDisconnect:
            del self.authenticated_clients[websocket]
    
    async def handle_authenticated_message(self, websocket: WebSocket, data: dict):
        """Handle messages from authenticated users"""
        user = self.authenticated_clients.get(websocket)
        
        # Check permissions
        if data["type"] == "admin_command" and "admin" not in user.roles:
            await websocket.send_json({
                "type": "error",
                "message": "Insufficient permissions"
            })
            return
        
        # Process message
        # ...
```

## Broadcasting Patterns

### Broadcast to All Clients

```python
class BroadcastService(CliffracerService, WebSocketMixin):
    async def broadcast_to_all(self, message: dict):
        """Send message to all connected clients"""
        disconnected = []
        
        for websocket in self._websocket_clients:
            try:
                await websocket.send_json(message)
            except:
                disconnected.append(websocket)
        
        # Clean up disconnected clients
        for ws in disconnected:
            self._websocket_clients.remove(ws)
    
    @self.rpc
    async def send_announcement(self, text: str):
        """RPC method to send announcement to all WebSocket clients"""
        await self.broadcast_to_all({
            "type": "announcement",
            "text": text,
            "timestamp": datetime.now().isoformat()
        })
```

### Selective Broadcasting

```python
class TargetedBroadcastService(CliffracerService, WebSocketMixin):
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
class HeartbeatService(CliffracerService, WebSocketMixin):
    def __init__(self):
        super().__init__(config)
        self.client_last_seen = {}
    
    async def start_heartbeat(self):
        """Send periodic heartbeat to all clients"""
        while True:
            await asyncio.sleep(30)  # Every 30 seconds
            
            disconnected = []
            for ws in self._websocket_clients:
                try:
                    await ws.send_json({"type": "ping"})
                except:
                    disconnected.append(ws)
            
            # Clean up
            for ws in disconnected:
                self._websocket_clients.remove(ws)
    
    async def on_websocket_message(self, websocket, data):
        if data.get("type") == "pong":
            self.client_last_seen[websocket] = datetime.now()
```

### Rate Limiting

Prevent WebSocket abuse:

```python
from collections import defaultdict
from datetime import datetime, timedelta

class RateLimitedWebSocketService(CliffracerService, WebSocketMixin):
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
    client = TestClient(service.app)
    
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
```python
# Implement automatic reconnection on client
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
    self._websocket_clients.discard(websocket)
    self.authenticated_clients.pop(websocket, None)
    
    # Clean up subscriptions
    for topic, subscribers in self.subscriptions.items():
        subscribers.discard(websocket)
```

### CORS Issues
```python
# Configure CORS for WebSocket connections
from fastapi.middleware.cors import CORSMiddleware

self.app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```