# NATS Microservices Framework

> **Note**: This microservices framework has been separated from the main CultKu haiku processing project. See the [main README](../README.md) for the haiku/Reddit functionality.

A comprehensive, production-ready microservices framework built on [NATS](https://nats.io) with integrated monitoring, structured logging, and comprehensive testing.

## 🚀 Features

- **High-Performance Messaging**: Built on NATS for microsecond latency
- **Developer-Friendly**: Decorator-based service definitions inspired by Nameko
- **Type-Safe**: Pydantic schema validation for all service communication
- **Multi-Protocol**: NATS, HTTP/REST, and WebSocket support
- **Production Monitoring**: Zabbix integration with pre-built dashboards
- **Structured Logging**: JSON logs with Loguru and contextual information
- **Comprehensive Testing**: Unit and integration tests with pytest
- **Container-Ready**: Docker and Docker Compose configurations

## 📖 Documentation

- **[Getting Started](docs/getting-started/installation.md)**: Installation and setup
- **[Quick Start](docs/getting-started/quickstart.md)**: Build your first service
- **[Full Documentation](docs/)**: Complete guides and API reference

## 🎯 Quick Example

```python
from nats_service_extended import HTTPService, ServiceConfig
from nats_service_extended import validated_rpc, broadcast, listener
from pydantic import BaseModel

class CreateUserRequest(BaseModel):
    username: str
    email: str

class UserCreatedEvent(BaseModel):
    user_id: str
    username: str

class UserService(HTTPService):
    @validated_rpc(CreateUserRequest, dict)
    async def create_user(self, request: CreateUserRequest):
        user_id = f"user_{hash(request.username)}"
        await self.broadcast_user_created(user_id, request.username)
        return {"user_id": user_id, "status": "created"}
    
    @broadcast(UserCreatedEvent)
    async def broadcast_user_created(self, user_id: str, username: str):
        return UserCreatedEvent(user_id=user_id, username=username)

class NotificationService(HTTPService):
    @listener(UserCreatedEvent)
    async def on_user_created(self, event: UserCreatedEvent):
        print(f"Welcome {event.username}!")
```

## 🚀 Quick Start

### 1. Installation

```bash
# Clone repository
git clone https://github.com/your-username/nats-microservices.git
cd nats-microservices

# Set up Python environment (uv will automatically use .python-version)
# Ensure you have Python 3.13.2+ available via pyenv

# Install dependencies with uv (includes virtual environment creation)
uv sync --extra dev --extra monitoring

# Alternative: Install different dependency groups
uv sync --extra extended          # Basic HTTP/WebSocket support
uv sync --extra aws              # AWS messaging backend
uv sync --extra all              # All features
```

### 2. Start Services

```bash
# Start NATS server
docker run -d --name nats-server -p 4222:4222 -p 8222:8222 nats:alpine -js -m 8222

# Run example services (uv automatically activates the virtual environment)
uv run python example_extended_services.py
```

### 3. Test Your Services

```bash
# Create a user
curl -X POST "http://localhost:8001/api/users" \
  -H "Content-Type: application/json" \
  -d '{"username": "johndoe", "email": "john@example.com", "full_name": "John Doe"}'

# Check API docs
open http://localhost:8001/docs
```

## 🐳 Docker Deployment

### Full Stack with Monitoring

```bash
# Start everything (NATS, Zabbix, PostgreSQL, services)
docker-compose -f docker-compose-monitoring.yml up -d

# Access services:
# - NATS monitoring: http://localhost:8222
# - Zabbix dashboard: http://localhost:8080 (admin/zabbix)
# - User service API: http://localhost:8001/docs
# - Notification service: http://localhost:8002/docs
```

## 🧪 Testing

Comprehensive test suite included:

```bash
# Run all tests
uv run pytest

# Run only unit tests
uv run pytest tests/unit/

# Run integration tests (requires NATS)
uv run pytest tests/integration/

# Run with coverage
uv run pytest --cov=nats_service --cov-report=html
```

## 📁 Project Structure

```
cultku/
├── nats_service.py              # Core NATS service framework
├── nats_service_extended.py     # Extended features (HTTP, WebSocket, validation)
├── nats_service_logged.py       # Logged service variants
├── nats_runner.py               # Service runners with auto-restart
├── logging_config.py            # Structured logging configuration
├── example_extended_services.py # Example services
├── monitoring/                  # Monitoring and metrics
│   ├── metrics_service.py       # Metrics collection service
│   └── zabbix/                  # Zabbix configuration
├── tests/                       # Test suite
│   ├── unit/                    # Unit tests
│   └── integration/             # Integration tests
├── docs/                        # Documentation
└── docker-compose-monitoring.yml # Full stack deployment
```

## 🔗 Core Components

### Service Types

- **`NatsService`**: Base service with NATS messaging
- **`ExtendedService`**: Adds schema validation and broadcast/listener patterns
- **`HTTPService`**: Adds FastAPI HTTP endpoints
- **`WebSocketService`**: Adds WebSocket support
- **`LoggedService`**: Variants with structured logging

### Decorators

- **`@rpc`**: Define synchronous RPC methods (wait for response)
- **`@async_rpc`**: Define asynchronous RPC methods (fire-and-forget)
- **`@validated_rpc`**: RPC with Pydantic validation
- **`@event_handler`**: Handle NATS events
- **`@listener`**: Listen for typed broadcast messages
- **`@broadcast`**: Broadcast typed messages to all listeners

### Patterns

- **Synchronous RPC**: Request-response pattern with `call_rpc()` (waits for response)
- **Asynchronous RPC**: Fire-and-forget pattern with `call_async()` (no response)
- **Publish-Subscribe**: Event broadcasting and listening
- **Event Sourcing**: Event-driven architecture patterns
- **Circuit Breakers**: Fault tolerance patterns

## 🛡️ Production Features

- **Auto-Restart**: Services automatically restart on failure
- **Graceful Shutdown**: Proper cleanup on container stop
- **Health Checks**: Built-in health and readiness endpoints
- **Metrics Export**: Prometheus-compatible metrics
- **Log Aggregation**: Structured logs for centralized collection
- **Connection Pooling**: Efficient NATS connection management

## 📚 Examples

Check out the examples directory for:

- **Basic Services**: Simple RPC and event examples
- **E-commerce System**: Complex multi-service example
- **Monitoring Setup**: Complete monitoring configuration
- **Testing Examples**: Unit and integration test examples

## 💻 Development

### Setting up for Development

```bash
# Install all development dependencies
uv sync --extra all

# Run linting and formatting
uv run ruff check .
uv run ruff format .

# Run type checking
uv run mypy .

# Add a new dependency
uv add "new-package>=1.0.0"

# Add a development dependency
uv add --dev "dev-package>=1.0.0"
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests
5. Run `uv run pytest` and `uv run ruff check .`
6. Update documentation
7. Submit a pull request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🔗 Links

- **Documentation**: [Full Documentation](docs/)
- **NATS.io**: [Official NATS Documentation](https://docs.nats.io/)
- **Zabbix**: [Zabbix Documentation](https://www.zabbix.com/documentation)
- **FastAPI**: [FastAPI Documentation](https://fastapi.tiangolo.com/)
- **Pydantic**: [Pydantic Documentation](https://pydantic-docs.helpmanual.io/)

## 🆘 Support

- **Documentation**: Check the [docs](docs/) directory
- **Issues**: [GitHub Issues](https://github.com/your-username/cultku/issues)
- **Discussions**: [GitHub Discussions](https://github.com/your-username/cultku/discussions)
- **Email**: support@your-domain.com