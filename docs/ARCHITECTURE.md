# Cliffracer Architecture Guide

This document provides a comprehensive overview of the Cliffracer microservices framework architecture, design decisions, and implementation patterns.

## Overview

Cliffracer is a production-ready Python microservices framework built on NATS messaging with integrated HTTP, WebSocket, and database capabilities. It emphasizes security, performance, and developer experience.

## Core Architecture

### Service Foundation

```
CliffracerService (Base Service)
├── NATS Integration (messaging, RPC, events)
├── Lifecycle Management (startup, shutdown, health)
├── Configuration Management (environment, validation)
└── Error Handling (exceptions, recovery, logging)
```

### Mixin-Based Composition

The framework uses mixins to provide optional functionality:

```python
from cliffracer import CliffracerService
from cliffracer.core.mixins import HTTPMixin, WebSocketMixin, PerformanceMixin

class MyService(CliffracerService, HTTPMixin, PerformanceMixin):
    """Service with HTTP endpoints and performance optimizations"""
    pass
```

Available mixins:
- **HTTPMixin**: FastAPI integration with REST endpoints
- **WebSocketMixin**: Real-time bidirectional communication
- **ValidationMixin**: Enhanced input validation
- **PerformanceMixin**: Connection pooling and batch processing
- **DatabaseMixin**: Repository pattern integration

### Security Architecture

```
Security Layer
├── Authentication (JWT-based with SimpleAuthService)
├── Authorization (role/permission-based access control)
├── Input Validation (comprehensive parameter validation)
├── SQL Injection Protection (SecureRepository with whitelisting)
└── Correlation Tracking (distributed request tracing)
```

#### Authentication Flow
1. Client authenticates with username/password
2. Service returns JWT token with user claims
3. Subsequent requests include token in Authorization header
4. Middleware validates token and sets authentication context
5. Protected endpoints check authentication/authorization

#### SQL Injection Protection
- **Table Whitelisting**: Only pre-approved tables allowed
- **Field Validation**: Field names validated against model schemas
- **Parameter Sanitization**: Suspicious patterns detected and blocked
- **Parameterized Queries**: All values passed as parameters, never interpolated

### Performance Architecture

```
Performance Layer
├── Connection Pooling (database and messaging)
├── Batch Processing (bulk operations with configurable sizes)
├── Resource Management (async task tracking and cleanup)
├── Correlation Tracking (zero-overhead request tracing)
└── Metrics Collection (performance monitoring)
```

#### Performance Characteristics
- **Throughput**: 1,800+ requests/second
- **Latency**: 3.3ms average response time
- **Memory**: <1MB growth under sustained load
- **Efficiency**: 4.6x improvement with connection pooling

## Data Flow Architecture

### Request Flow (HTTP)
```
Client Request
├── HTTP Middleware (CORS, auth, correlation)
├── FastAPI Router (endpoint routing)
├── Service Handler (business logic)
├── Repository (data access with security)
├── Database (PostgreSQL with connection pooling)
└── Response (JSON with correlation headers)
```

### Message Flow (NATS)
```
Publisher Service
├── Event/RPC Call (with correlation ID)
├── NATS Client (message serialization)
├── NATS Server (routing and delivery)
├── Subscriber Service (message handling)
├── Handler Function (business logic)
└── Response/Acknowledgment (completion)
```

### WebSocket Flow
```
Client Connection
├── WebSocket Handshake (auth and correlation)
├── Connection Manager (active connection tracking)
├── NATS Event Subscription (backend integration)
├── Message Broadcasting (real-time updates)
└── Client Notification (JSON message delivery)
```

## Database Architecture

### Repository Pattern
```python
from cliffracer.database import SecureRepository
from cliffracer.database.models import User

class UserService(CliffracerService):
    def __init__(self):
        super().__init__(name="user_service")
        self.users = SecureRepository(User)

    @self.rpc
    async def create_user(self, name: str, email: str) -> dict:
        user = User(name=name, email=email)
        return await self.users.create(user)
```

### Model Definition
```python
from cliffracer.database.models import DatabaseModel
from pydantic import Field

class User(DatabaseModel):
    __tablename__ = "users"
    
    name: str = Field(..., description="User full name")
    email: str = Field(..., description="User email address")
    status: str = Field(default="active", description="User status")
```

### Security Features
- **SecureRepository**: Prevents SQL injection with table/field validation
- **Input Validation**: All parameters validated before database operations
- **Connection Pooling**: Efficient database connection management
- **Transaction Support**: Context managers for atomic operations

## Messaging Architecture

### NATS Integration
```
NATS Server
├── Core NATS (pub/sub, request/reply)
├── JetStream (persistence, replay, delivery guarantees)
├── KV Store (configuration and state management)
└── Object Store (large data handling)
```

### RPC Communication
```python
# Service A (caller)
@service.rpc
async def process_order(self, order_data: dict) -> dict:
    # Call another service
    payment_result = await self.call_rpc(
        "payment_service", 
        "process_payment", 
        amount=order_data["total"]
    )
    return {"order_id": "123", "payment": payment_result}

# Service B (handler)
@payment_service.rpc
async def process_payment(self, amount: float) -> dict:
    # Process payment
    return {"transaction_id": "tx_456", "status": "completed"}
```

### Event-Driven Communication
```python
# Publisher
@service.rpc
async def create_user(self, user_data: dict) -> dict:
    user = await self.users.create(User(**user_data))
    
    # Publish event
    await self.publish_event("user.created", user.model_dump())
    return user.model_dump()

# Subscriber
@service.event("user.created")
async def on_user_created(self, user_data: dict):
    # Send welcome email
    await self.send_welcome_email(user_data["email"])
```

## Observability Architecture

### Correlation Tracking
```
Request Flow
├── Correlation ID Generation (auto or header-based)
├── Context Propagation (across service boundaries)
├── Logging Integration (structured logs with correlation)
├── Error Tracking (distributed error attribution)
└── Performance Monitoring (request-level metrics)
```

### Logging Integration
```python
from cliffracer.logging import get_correlation_logger

logger = get_correlation_logger(__name__)

@service.rpc
async def process_request(self, data: dict) -> dict:
    # Logs automatically include correlation ID
    logger.info("Processing request", extra={"data_size": len(data)})
    
    result = await self.process_data(data)
    
    logger.info("Request completed", extra={"result_type": type(result)})
    return result
```

### Health Monitoring
```python
# Automatic health endpoints
GET /health -> {"status": "healthy", "service": "user_service"}
GET /health/detailed -> {
    "status": "healthy",
    "database": "connected",
    "nats": "connected",
    "metrics": {"rps": 150, "latency_ms": 3.2}
}
```

## Development Architecture

### Service Development Lifecycle
1. **Define Service**: Create service class with mixins
2. **Implement Handlers**: Add RPC and event handlers
3. **Add Database Models**: Define Pydantic models for data
4. **Configure Security**: Set up authentication and validation
5. **Write Tests**: Unit, integration, and performance tests
6. **Deploy**: Docker containers with health checks

### Testing Strategy
```
Testing Pyramid
├── Unit Tests (individual components, mocked dependencies)
├── Integration Tests (service communication, database operations)
├── Performance Tests (load testing, benchmarks)
├── Security Tests (authentication, authorization, injection)
└── End-to-End Tests (complete user workflows)
```

### Configuration Management
```python
from cliffracer import ServiceConfig

config = ServiceConfig(
    name="user_service",
    nats_url="nats://localhost:4222",
    
    # Database configuration
    db_host="localhost",
    db_name="cliffracer",
    
    # Security configuration
    auth_secret_key="your-secret-key",
    enable_auth=True,
    
    # Performance configuration
    connection_pool_size=10,
    batch_size=100,
    enable_metrics=True
)

service = CliffracerService(config)
```

## Deployment Architecture

### Container Strategy
```dockerfile
FROM python:3.11-slim

# Install dependencies
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --no-dev

# Copy application
COPY src/ ./src/

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s \
  CMD curl -f http://localhost:8080/health || exit 1

# Run service
CMD ["python", "-m", "src.your_service"]
```

### Kubernetes Deployment
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: user-service
spec:
  replicas: 3
  selector:
    matchLabels:
      app: user-service
  template:
    metadata:
      labels:
        app: user-service
    spec:
      containers:
      - name: user-service
        image: cliffracer/user-service:1.0.0
        ports:
        - containerPort: 8080
        env:
        - name: NATS_URL
          value: "nats://nats-service:4222"
        - name: DB_HOST
          value: "postgres-service"
        readinessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 10
```

### Production Considerations

#### Security
- All secrets in environment variables or secret management
- Network policies for service-to-service communication
- TLS encryption for external communications
- Regular security audits and dependency updates

#### Performance
- Horizontal pod autoscaling based on CPU/memory
- Connection pooling tuned for expected load
- Batch processing for bulk operations
- Monitoring and alerting on performance metrics

#### Reliability
- Circuit breakers for external service calls
- Retries with exponential backoff
- Graceful degradation for non-critical features
- Comprehensive error handling and recovery

## Design Decisions

### Why NATS?
- **Performance**: Sub-millisecond latency, millions of messages/second
- **Simplicity**: Simple protocol, easy to understand and debug
- **Features**: Core messaging plus JetStream for persistence
- **Ecosystem**: Good Python client support and observability tools

### Why Mixins Over Inheritance?
- **Flexibility**: Compose only needed features
- **Testability**: Easier to test individual components
- **Maintainability**: Clear separation of concerns
- **Extensibility**: Easy to add new features without breaking existing code

### Why Pydantic Models?
- **Type Safety**: Runtime validation with type hints
- **Serialization**: Automatic JSON serialization/deserialization
- **Documentation**: Self-documenting schemas
- **Integration**: Works well with FastAPI and databases

### Why PostgreSQL?
- **ACID Compliance**: Strong consistency guarantees
- **Performance**: Excellent performance for complex queries
- **Features**: Rich data types, JSON support, full-text search
- **Ecosystem**: Mature tooling and extension ecosystem

## Future Architecture

### Planned Enhancements
- **Observability**: OpenTelemetry integration for distributed tracing
- **Messaging**: Additional backends (Redis Streams, Apache Kafka)
- **Security**: OAuth2/OIDC integration for enterprise authentication
- **Performance**: gRPC support for high-performance communication
- **Deployment**: Kubernetes operator for automated deployment

### Scalability Roadmap
- **Horizontal Scaling**: Automatic service scaling based on metrics
- **Data Partitioning**: Sharding strategies for large datasets
- **Caching**: Redis integration for performance optimization
- **CDN Integration**: Static asset delivery optimization

---

This architecture provides a solid foundation for building production-ready microservices with excellent performance, security, and developer experience characteristics.