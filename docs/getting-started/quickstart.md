# Quick Start

Get up and running with the NATS Microservices Framework in under 10 minutes.

## Prerequisites

Make sure you have completed the [installation](installation.md) and have:

- Python 3.11+ environment activated
- NATS server running (Docker or local)
- Framework dependencies installed

## Your First Service

Let's build a simple user management service with RPC and event broadcasting.

### 1. Create a Basic Service

Create `my_first_service.py`:

```python
import asyncio
from datetime import datetime
from typing import List

from nats_service_extended import HTTPService, ServiceConfig
from nats_service_extended import validated_rpc, broadcast, listener
from nats_service_extended import RPCRequest, RPCResponse, BroadcastMessage
from pydantic import BaseModel, Field

# Define message schemas
class CreateUserRequest(RPCRequest):
    """Request to create a new user"""
    username: str = Field(..., min_length=3, max_length=50)
    email: str = Field(..., pattern=r'^[\w\.-]+@[\w\.-]+\.\w+$')
    full_name: str

class UserResponse(RPCResponse):
    """User data response"""
    user_id: str
    username: str
    email: str
    full_name: str
    created_at: datetime

class UserCreatedEvent(BroadcastMessage):
    """Event broadcasted when user is created"""
    user_id: str
    username: str
    email: str

# Create the service
class UserService(HTTPService):
    """User management service with HTTP API"""
    
    def __init__(self):
        config = ServiceConfig(name="user_service")
        super().__init__(config, port=8001)
        
        # In-memory user storage (use database in production)
        self.users = {}
        self.user_counter = 0
        
        # Add HTTP endpoints
        @self.post("/api/users", response_model=UserResponse)
        async def create_user_http(request: CreateUserRequest):
            """HTTP endpoint to create user"""
            return await self.create_user(request)
            
        @self.get("/api/users/{user_id}", response_model=UserResponse)
        async def get_user_http(user_id: str):
            """HTTP endpoint to get user"""
            return await self.get_user(user_id)
    
    @validated_rpc(CreateUserRequest, UserResponse)
    async def create_user(self, request: CreateUserRequest) -> UserResponse:
        """Create a new user"""
        # Generate user ID
        self.user_counter += 1
        user_id = f"user_{self.user_counter}"
        
        # Store user
        user_data = {
            "user_id": user_id,
            "username": request.username,
            "email": request.email,
            "full_name": request.full_name,
            "created_at": datetime.utcnow()
        }
        self.users[user_id] = user_data
        
        # Broadcast user created event
        await self.broadcast_user_created(user_id, request.username, request.email)
        
        return UserResponse(**user_data)
    
    async def get_user(self, user_id: str) -> UserResponse:
        """Get user by ID"""
        user_data = self.users.get(user_id)
        if not user_data:
            raise ValueError(f"User {user_id} not found")
        return UserResponse(**user_data)
    
    @broadcast(UserCreatedEvent)
    async def broadcast_user_created(self, user_id: str, username: str, email: str):
        """Broadcast user creation event"""
        return UserCreatedEvent(
            user_id=user_id,
            username=username,
            email=email,
            source_service=self.config.name
        )

if __name__ == "__main__":
    from nats_runner import ServiceRunner, configure_logging
    
    # Configure logging
    configure_logging()
    
    # Create and run service
    config = ServiceConfig(name="user_service", auto_restart=True)
    runner = ServiceRunner(UserService, config)
    runner.run_forever()
```

### 2. Run Your Service

```bash
# Make sure NATS is running
docker run -d --name nats-server -p 4222:4222 -p 8222:8222 nats:alpine -js -m 8222

# Run your service
python my_first_service.py
```

You should see output like:

```
2024-01-01 12:00:00 - nats_runner - INFO - Starting runner for service 'user_service'
2024-01-01 12:00:00 - nats_service - INFO - Service 'user_service' connected to NATS at nats://localhost:4222
2024-01-01 12:00:00 - nats_service - INFO - Service 'user_service' started with 1 RPC handlers and 0 event handlers
2024-01-01 12:00:00 - uvicorn - INFO - Application startup complete.
2024-01-01 12:00:00 - uvicorn - INFO - Uvicorn running on http://0.0.0.0:8001
```

### 3. Test Your Service

#### Via HTTP API

```bash
# Create a user via HTTP
curl -X POST "http://localhost:8001/api/users" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "johndoe",
    "email": "john@example.com",
    "full_name": "John Doe"
  }'

# Response:
# {
#   "user_id": "user_1",
#   "username": "johndoe",
#   "email": "john@example.com",
#   "full_name": "John Doe",
#   "created_at": "2024-01-01T12:00:00.123456",
#   "success": true,
#   "error": null,
#   "timestamp": "2024-01-01T12:00:00.123456"
# }

# Get user details
curl "http://localhost:8001/api/users/user_1"

# Check API documentation
open http://localhost:8001/docs
```

#### Via RPC (from another service)

Create `test_client.py`:

```python
import asyncio
from nats_service import Service, ServiceConfig

async def test_rpc():
    # Create client service
    config = ServiceConfig(name="test_client")
    client = Service(config)
    
    await client.connect()
    
    try:
        # Call RPC method
        response = await client.call_rpc(
            "user_service",
            "create_user",
            username="janedoe",
            email="jane@example.com",
            full_name="Jane Doe"
        )
        
        print(f"Created user: {response}")
        
        # Get user via RPC
        user_details = await client.call_rpc(
            "user_service",
            "get_user",
            user_id=response["user_id"]
        )
        
        print(f"User details: {user_details}")
        
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(test_rpc())
```

```bash
python test_client.py
```

## Add Event Listening

Let's create a notification service that listens for user creation events.

### 1. Create Notification Service

Create `notification_service.py`:

```python
from nats_service_extended import ExtendedService, ServiceConfig, listener
from my_first_service import UserCreatedEvent

class NotificationService(ExtendedService):
    """Service that handles notifications"""
    
    def __init__(self):
        config = ServiceConfig(name="notification_service")
        super().__init__(config)
        self.notifications = []
    
    @listener(UserCreatedEvent)
    async def on_user_created(self, event: UserCreatedEvent):
        """Handle user creation events"""
        # Create notification
        notification = {
            "type": "welcome_email",
            "user_id": event.user_id,
            "username": event.username,
            "email": event.email,
            "message": f"Welcome {event.username}! Your account has been created.",
            "sent_at": event.timestamp.isoformat()
        }
        
        self.notifications.append(notification)
        
        # In production, send actual email here
        print(f"📧 Sending welcome email to {event.username} ({event.email})")
        
        # Could also call external service
        # await self.email_service.send_welcome_email(event.email, event.username)

if __name__ == "__main__":
    from nats_runner import ServiceRunner, configure_logging
    
    configure_logging()
    
    config = ServiceConfig(name="notification_service", auto_restart=True)
    runner = ServiceRunner(NotificationService, config)
    runner.run_forever()
```

### 2. Run Multiple Services

```bash
# Terminal 1: User Service
python my_first_service.py

# Terminal 2: Notification Service
python notification_service.py

# Terminal 3: Test
curl -X POST "http://localhost:8001/api/users" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "alice",
    "email": "alice@example.com", 
    "full_name": "Alice Smith"
  }'
```

You should see the notification service automatically receive and process the user creation event!

## Monitor Your Services

### 1. Check Service Health

```bash
# User service health
curl http://localhost:8001/health

# Service info
curl http://localhost:8001/info
```

### 2. Monitor NATS

```bash
# NATS monitoring dashboard
open http://localhost:8222

# Connection info
curl http://localhost:8222/connz

# Server stats
curl http://localhost:8222/varz
```

### 3. View Logs

```bash
# Check logs directory
ls -la logs/

# View user service logs
tail -f logs/user_service.log

# View notification service logs  
tail -f logs/notification_service.log
```

## Add Validation and Error Handling

Let's see how the framework handles validation errors:

```bash
# Try creating user with invalid data
curl -X POST "http://localhost:8001/api/users" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "ab",
    "email": "invalid-email",
    "full_name": ""
  }'

# Response will include validation errors:
# {
#   "success": false,
#   "error": "Validation error",
#   "traceback": "username: ensure this value has at least 3 characters..."
# }
```

## Next Steps

Congratulations! You've built your first NATS microservices. Here's what to explore next:

### Immediate Next Steps

1. **[Add WebSocket Support](../extended/websocket-services.md)**: Real-time communication
2. **[Set Up Monitoring](../monitoring/zabbix.md)**: Production monitoring with Zabbix
3. **[Add Database Integration](../examples/ecommerce-system.md)**: Persistent storage
4. **[Write Tests](../testing/unit-tests.md)**: Test your services

### Production Considerations

1. **[Deployment](../deployment/docker-compose.md)**: Deploy with Docker Compose
2. **[Logging Configuration](../logging/configuration.md)**: Production logging setup
3. **[Security](../deployment/production.md#security)**: Authentication and authorization
4. **[Scaling](../deployment/production.md#scaling)**: Multi-instance deployment

### Advanced Features

1. **[Schema Evolution](../framework/schema-validation.md)**: Handling schema changes
2. **[Circuit Breakers](../framework/message-patterns.md#circuit-breakers)**: Fault tolerance
3. **[Distributed Tracing](../monitoring/metrics.md#tracing)**: Request tracking
4. **[Event Sourcing](../examples/ecommerce-system.md#event-sourcing)**: Event-driven patterns

## Common Patterns

### Request-Response Pattern

```python
@validated_rpc(ProcessOrderRequest, ProcessOrderResponse)
async def process_order(self, request: ProcessOrderRequest) -> ProcessOrderResponse:
    # Validate inventory
    inventory_check = await self.call_rpc(
        "inventory_service", 
        "check_availability", 
        items=request.items
    )
    
    if not inventory_check["available"]:
        raise ValueError("Items not available")
    
    # Process payment
    payment_result = await self.call_rpc(
        "payment_service",
        "charge_customer",
        customer_id=request.customer_id,
        amount=request.total
    )
    
    # Create order
    order = await self.create_order(request)
    
    return ProcessOrderResponse(order_id=order.id, status="completed")
```

### Event-Driven Pattern

```python
@listener(OrderCreatedEvent)
async def on_order_created(self, event: OrderCreatedEvent):
    # Update inventory
    await self.call_rpc(
        "inventory_service",
        "reserve_items", 
        order_id=event.order_id,
        items=event.items
    )
    
    # Send confirmation email
    await self.broadcast_order_confirmation(event.order_id, event.customer_id)

@broadcast(OrderConfirmationEvent)
async def broadcast_order_confirmation(self, order_id: str, customer_id: str):
    return OrderConfirmationEvent(
        order_id=order_id,
        customer_id=customer_id,
        source_service=self.config.name
    )
```

### Error Handling Pattern

```python
@validated_rpc(ProcessRequestRequest, ProcessRequestResponse)
async def process_request(self, request: ProcessRequestRequest) -> ProcessRequestResponse:
    try:
        # Attempt operation
        result = await self.risky_operation(request)
        
        return ProcessRequestResponse(
            success=True,
            data=result
        )
        
    except ValidationError as e:
        # Handle validation errors
        return ProcessRequestResponse(
            success=False,
            error="Validation failed",
            details=str(e)
        )
        
    except TimeoutError:
        # Handle timeout errors
        return ProcessRequestResponse(
            success=False,
            error="Operation timed out",
            retry_after=30
        )
        
    except Exception as e:
        # Handle unexpected errors
        self.logger.exception("Unexpected error in process_request")
        return ProcessRequestResponse(
            success=False,
            error="Internal server error",
            correlation_id=request.correlation_id
        )
```

You're now ready to build production-ready microservices with the NATS framework!