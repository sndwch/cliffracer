# Examples Directory

This directory contains comprehensive examples demonstrating all features of the NATS Microservices Framework. Each example includes complete code, documentation, and usage instructions.

## Quick Start Examples

### [Basic Services](basic-services.md)
**File**: `../example_services.py`

Demonstrates core framework features with a simple e-commerce scenario:
- Order processing service
- Inventory management
- Notification system
- Event-driven communication
- Service orchestration

**Perfect for**: First-time users learning the framework basics

### [E-commerce System](ecommerce-system.md) 
**File**: `../example_extended_services.py`

Comprehensive user management system showcasing advanced features:
- HTTP/REST APIs with FastAPI
- Schema validation with Pydantic
- WebSocket real-time communication
- Broadcast/listener patterns
- Multi-protocol services

**Perfect for**: Understanding production-ready service architecture

## Advanced Examples

### [Authentication & RBAC](auth-patterns.md)
**File**: `../example_auth_patterns.py`

Complete authentication and authorization system:
- JWT token authentication
- Role-based access control (RBAC)
- Multiple authentication patterns
- HTTP middleware integration
- Context propagation
- Permission decorators

**Perfect for**: Adding security to your services

### [Async Communication Patterns](async-patterns.md)
**File**: `../example_async_patterns.py`

Different asynchronous communication patterns:
- Synchronous RPC (request-response)
- Asynchronous RPC (fire-and-forget)
- Event-driven architecture
- Performance comparisons
- Error handling strategies
- Batch processing

**Perfect for**: Optimizing service communication patterns

## Example Categories

### 🚀 **Getting Started**
- [Basic Services](basic-services.md) - Start here for framework introduction
- [E-commerce System](ecommerce-system.md) - Comprehensive example with HTTP APIs

### 🔒 **Security & Authentication**
- [Authentication Patterns](auth-patterns.md) - JWT, RBAC, and security middleware
- Session Management (coming soon)
- OAuth Integration (coming soon)

### ⚡ **Performance & Patterns**
- [Async Patterns](async-patterns.md) - Sync vs async communication
- Circuit Breakers (coming soon)
- Load Balancing (coming soon)

### 🔧 **Production Features**
- [Monitoring Setup](monitoring-setup.md) - Zabbix integration (coming soon)
- Database Integration (coming soon)
- Error Handling (coming soon)

### 🧪 **Testing**
- Unit Testing (see `../tests/unit/`)
- Integration Testing (see `../tests/integration/`)
- Load Testing (coming soon)

## How to Use These Examples

### 1. Prerequisites

Ensure you have the framework installed and NATS running:

```bash
# Install dependencies
pip install -r requirements-monitoring.txt

# Start NATS server
docker run -d --name nats-server \
  -p 4222:4222 -p 8222:8222 \
  nats:alpine -js -m 8222
```

### 2. Run an Example

```bash
# Basic services example
python example_services.py

# Extended services with HTTP APIs
python example_extended_services.py

# Authentication patterns
python example_auth_patterns.py

# Async communication patterns
python example_async_patterns.py
```

### 3. Test the Examples

Most examples include built-in test clients:

```bash
# Run test client for basic services
python example_services.py test

# Run test client for extended services
python example_extended_services.py test

# Run authentication demo
python example_auth_patterns.py

# Run async patterns demo
python example_async_patterns.py
```

### 4. Explore HTTP APIs

For examples with HTTP services, check the interactive documentation:

- **User Service**: http://localhost:8001/docs
- **Notification Service**: http://localhost:8002/docs

## Example Code Organization

Each example follows a consistent structure:

```python
# 1. Message schemas (Pydantic models)
class CreateUserRequest(RPCRequest):
    username: str
    email: str

# 2. Service classes
class UserService(HTTPService):
    @validated_rpc(CreateUserRequest, UserResponse)
    async def create_user(self, request: CreateUserRequest):
        # Service logic
        pass

# 3. Event handlers
@listener(UserCreatedEvent)
async def on_user_created(self, event: UserCreatedEvent):
    # Event handling logic
    pass

# 4. Test/demo functions
async def test_services():
    # Example usage and testing
    pass

# 5. Main execution
if __name__ == "__main__":
    # Service runner setup
    pass
```

## Learning Path

Recommended order for exploring the examples:

1. **[Basic Services](basic-services.md)** - Learn core concepts
2. **[E-commerce System](ecommerce-system.md)** - Understand HTTP integration
3. **[Async Patterns](async-patterns.md)** - Master communication patterns
4. **[Authentication](auth-patterns.md)** - Add security features
5. **Production Examples** - Deploy and monitor services

## Common Patterns Demonstrated

### Service Definition

```python
class MyService(HTTPService):
    def __init__(self):
        config = ServiceConfig(name="my_service")
        super().__init__(config, port=8001)
    
    @validated_rpc(RequestModel, ResponseModel)
    async def my_method(self, request: RequestModel) -> ResponseModel:
        # Implementation
        pass
```

### Event Broadcasting

```python
@broadcast(EventModel)
async def broadcast_event(self, data: str) -> EventModel:
    return EventModel(data=data, timestamp=datetime.utcnow())
```

### Event Listening

```python
@listener(EventModel)
async def handle_event(self, event: EventModel):
    print(f"Received event: {event.data}")
```

### HTTP Integration

```python
@self.post("/api/endpoint")
async def http_endpoint(request: RequestModel):
    return await self.rpc_method(request)
```

## Troubleshooting

### Common Issues

1. **NATS Connection Failed**
   ```bash
   # Check NATS is running
   docker ps | grep nats
   curl http://localhost:8222/varz
   ```

2. **Import Errors**
   ```bash
   # Verify installation
   python -c "from nats_service import Service; print('✅ Framework ready')"
   ```

3. **Port Already in Use**
   ```bash
   # Find and kill process using port
   lsof -ti:8001 | xargs kill
   ```

4. **Schema Validation Errors**
   - Check Pydantic model definitions
   - Ensure request data matches schema
   - Verify field types and constraints

### Debug Mode

Enable debug logging for troubleshooting:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## Contributing Examples

We welcome contributions of new examples! Please:

1. Follow the existing code structure
2. Include comprehensive documentation
3. Add test/demo functions
4. Update this README with your example
5. Submit a pull request

### Example Template

```python
"""
Example: [Your Example Name]
Demonstrates: [Key features demonstrated]
"""

# Your example code here...

if __name__ == "__main__":
    # Demo/test code
    pass
```

## Additional Resources

- **[Framework Documentation](../docs/)** - Complete API reference
- **[Production Deployment](../docs/deployment/)** - Production best practices
- **[Monitoring Guide](../docs/monitoring/)** - Observability and metrics
- **[Testing Guide](../docs/testing/)** - Testing strategies
- **[NATS Documentation](https://docs.nats.io/)** - NATS server documentation

## Support

If you have questions about any example:

- 📖 Check the [main documentation](../docs/)
- 🐛 Report issues in [GitHub Issues](https://github.com/your-username/cultku/issues)
- 💬 Ask questions in [GitHub Discussions](https://github.com/your-username/cultku/discussions)
- 📧 Email support@your-domain.com
