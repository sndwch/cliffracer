# API Reference

Quick reference for the main Cliffracer classes and methods.

## Core Classes

### CliffracerService

The main service class for building microservices.

```python
from cliffracer import CliffracerService, ServiceConfig

class MyService(CliffracerService):
    def __init__(self):
        config = ServiceConfig(name="my_service")
        super().__init__(config)
```

**Key Methods:**
- `run()` - Start the service
- `stop()` - Stop the service gracefully
- `publish(subject: str, data: dict)` - Publish event
- `rpc_call(service: str, method: str, **kwargs)` - Call RPC method

**Decorators:**
- `@rpc` - Define RPC method
- `@event(pattern)` - Subscribe to events
- `@timer(seconds)` - Scheduled tasks

### ServiceConfig

Configuration for services.

```python
ServiceConfig(
    name: str,                    # Required: Service name
    nats_url: str = "nats://localhost:4222",
    enable_auth: bool = False,
    enable_metrics: bool = True,
    batch_size: int = 100,
    connection_pool_size: int = 10,
    timeout: float = 30.0
)
```

## Mixins

### HTTPMixin

Adds HTTP/REST capabilities via FastAPI.

```python
from cliffracer.core.mixins import HTTPMixin

class APIService(CliffracerService, HTTPMixin):
    def __init__(self):
        super().__init__(config)
        self._http_port = 8080  # Required
```

**Provides:**
- `self.app` - FastAPI instance
- HTTP decorators: `@get`, `@post`, `@put`, `@delete`
- Automatic correlation ID middleware
- Health check endpoint

### WebSocketMixin

Adds WebSocket support.

```python
from cliffracer.core.mixins import WebSocketMixin

class RealtimeService(CliffracerService, WebSocketMixin):
    def __init__(self):
        super().__init__(config)
        self._websocket_port = 8081  # Required
```

**Methods:**
- `websocket_broadcast(data: dict)` - Broadcast to all clients
- `on_websocket_connect(websocket)` - Handle new connections
- `on_websocket_disconnect(websocket)` - Handle disconnections
- `on_websocket_message(websocket, data)` - Handle messages

### PerformanceMixin

Adds performance optimizations.

```python
from cliffracer.core.mixins import PerformanceMixin

class FastService(CliffracerService, PerformanceMixin):
    pass
```

**Features:**
- Connection pooling
- Batch processing
- Performance metrics collection

### ValidationMixin

Adds enhanced input validation.

```python
from cliffracer.core.mixins import ValidationMixin

class SecureService(CliffracerService, ValidationMixin):
    pass
```

## Authentication

### SimpleAuthService

JWT-based authentication service.

```python
from cliffracer.auth.simple_auth import SimpleAuthService, AuthConfig

config = AuthConfig(
    secret_key="your-secret-key-min-32-chars",
    token_expiry_hours=24
)
auth = SimpleAuthService(config)
```

**Methods:**
- `create_user(username, email, password) -> AuthUser`
- `authenticate(username, password) -> str` (JWT token)
- `validate_token(token) -> AuthContext`
- `add_role(username, role)`
- `add_permission(role, permission)`

### AuthUser

User model for authentication.

```python
AuthUser(
    id: str,
    username: str,
    email: str,
    password_hash: str,
    roles: List[str] = [],
    permissions: List[str] = [],
    is_active: bool = True,
    created_at: datetime
)
```

## Database

### DatabaseModel

Base class for database models.

```python
from cliffracer.database.models import DatabaseModel
from pydantic import Field

class User(DatabaseModel):
    __tablename__ = "users"  # Required
    
    name: str = Field(..., description="User name")
    email: str = Field(..., description="Email address")
```

**Provides:**
- `id: UUID` - Auto-generated
- `created_at: datetime` - Auto-set
- `updated_at: datetime` - Auto-updated
- `dict_for_db() -> dict` - Convert for database
- `get_create_table_sql() -> str` - Generate CREATE TABLE

### Repository

Basic CRUD operations.

```python
from cliffracer.database import Repository

users = Repository(User)
```

**Methods:**
- `create(model) -> model`
- `get(id) -> model`
- `update(id, data) -> model`
- `delete(id) -> bool`
- `list(limit, offset) -> List[model]`
- `find_by_field(field, value) -> List[model]`

### SecureRepository

Repository with SQL injection protection.

```python
from cliffracer.database import SecureRepository

users = SecureRepository(User)
```

**Same methods as Repository but with:**
- Table name validation
- Field name validation
- SQL pattern detection
- Parameter sanitization

## Decorators

### RPC Decorators

```python
@rpc
async def my_method(self, param: str) -> dict:
    """Basic RPC method"""
    return {"result": param}

@validated_rpc
async def validated_method(self, email: str, age: int) -> dict:
    """RPC with automatic validation"""
    return {"email": email, "age": age}

@robust_rpc(retries=3, timeout=10.0)
async def reliable_method(self) -> dict:
    """RPC with retry logic"""
    return {"status": "ok"}
```

### Event Decorators

```python
@event("user.created")
async def on_user_created(self, data: dict):
    """Subscribe to specific event"""
    pass

@event("order.*")
async def on_any_order_event(self, data: dict):
    """Subscribe to pattern"""
    pass

@broadcast("notifications")
async def send_notification(self, message: str):
    """Broadcast to topic"""
    pass
```

### Timer Decorators

```python
@timer(60)  # Every 60 seconds
async def periodic_task(self):
    """Run periodically"""
    pass

@timer.cron("0 * * * *")  # Every hour
async def hourly_task(self):
    """Run on schedule"""
    pass
```

### HTTP Decorators

```python
@get("/users/{user_id}")
async def get_user(self, user_id: str):
    return {"user_id": user_id}

@post("/users", status_code=201)
async def create_user(self, user: UserModel):
    return {"id": "123"}

@put("/users/{user_id}")
async def update_user(self, user_id: str, user: UserModel):
    return {"status": "updated"}

@delete("/users/{user_id}", status_code=204)
async def delete_user(self, user_id: str):
    pass  # No content for 204
```

## Patterns

### Saga Pattern

Distributed transaction management.

```python
from cliffracer.patterns.saga import SagaCoordinator, SagaStep

coordinator = SagaCoordinator(service)

# Define saga
coordinator.define_saga("order_processing", [
    SagaStep(
        name="create_order",
        service="order_service",
        action="create_order",
        compensation="cancel_order",
        timeout=10.0,
        retry_count=3
    ),
    SagaStep(
        name="process_payment",
        service="payment_service",
        action="charge_card",
        compensation="refund_payment"
    )
])

# Execute saga
result = await coordinator.start_saga("order_processing", {
    "customer_id": "123",
    "amount": 99.99
})
```

## Correlation Tracking

### CorrelationContext

Track requests across services.

```python
from cliffracer import (
    get_correlation_id,
    set_correlation_id,
    create_correlation_id,
    with_correlation_id
)

# Get current ID
correlation_id = get_correlation_id()

# Set ID
set_correlation_id("custom-id-123")

# Create new ID
new_id = create_correlation_id()

# Context manager
async with with_correlation_id("request-123"):
    # All operations use this ID
    await service.process_request()
```

## Error Handling

### Exception Hierarchy

```python
from cliffracer.core.exceptions import (
    CliffracerError,          # Base exception
    ServiceError,             # Service-level errors
    ConnectionError,          # NATS connection issues
    ConfigurationError,       # Config problems
    ValidationError,          # Input validation
    AuthenticationError,      # Auth failures
    AuthorizationError,       # Permission denied
    DatabaseError,            # Database issues
    RPCError,                 # RPC call failures
)
```

### Error Handler

```python
from cliffracer.core.exceptions import ErrorHandler

# Global error handler
@service.error_handler(ValidationError)
async def handle_validation_error(error: ValidationError):
    logger.error(f"Validation failed: {error}")
    return {"error": "Invalid input", "details": str(error)}

# Method-specific handler
@service.rpc
@handle_errors(ValidationError, default={"error": "Invalid input"})
async def validate_data(self, data: dict):
    # Will return default on ValidationError
    pass
```

## Performance

### PerformanceMetrics

Track service performance.

```python
from cliffracer.performance import PerformanceMetrics

metrics = PerformanceMetrics()

# Record metric
metrics.record("request_count", 1)
metrics.record("response_time", 0.025)

# Get metrics
stats = metrics.get_stats()
# {
#   "request_count": {"total": 1000, "rate": 50.0},
#   "response_time": {"avg": 0.025, "min": 0.001, "max": 0.1}
# }
```

### BatchProcessor

Process items in batches.

```python
from cliffracer.performance import BatchProcessor

processor = BatchProcessor(
    batch_size=100,
    flush_interval=5.0,
    process_func=self.process_batch
)

# Add items
await processor.add_item(item)

# Process immediately
await processor.flush()
```

## Logging

### Service Logger

```python
from cliffracer.logging import get_service_logger

logger = get_service_logger("my_service")

# Logs include correlation ID automatically
logger.info("Processing request", extra={"user_id": "123"})
```

### LoggingConfig

```python
from cliffracer.logging import LoggingConfig

config = LoggingConfig(
    level="INFO",
    format="json",  # or "text"
    include_timestamp=True,
    include_correlation_id=True
)
```

## Utilities

### Input Validation

```python
from cliffracer.core.validation import (
    validate_port,      # 1-65535
    validate_timeout,   # > 0
    validate_email,     # Email format
    validate_url,       # URL format
)

port = validate_port(8080)
timeout = validate_timeout(30.0)
```

### Environment Helpers

```python
from cliffracer.utils import get_env

# With default
nats_url = get_env("NATS_URL", "nats://localhost:4222")

# Required
secret = get_env("SECRET_KEY", required=True)

# With type conversion
port = get_env("PORT", default=8080, type=int)
```