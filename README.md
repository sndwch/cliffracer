# Cliffracer

A production-ready NATS-based microservices framework for Python with HTTP, WebSocket, and database integration.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code Style: Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

## 🚀 Features

### Core Microservices Framework
- **NATS Integration**: High-performance messaging with JetStream support
- **RPC Communication**: Type-safe request/response patterns with timeout handling
- **Event Streaming**: Publish/subscribe patterns with pattern-based routing
- **Service Info**: Basic service information endpoint (get_service_info)
- **Connection Management**: Auto-reconnection, health monitoring, and graceful shutdown

### Web Integration
- **HTTP/REST APIs**: FastAPI integration with automatic OpenAPI documentation
- **WebSocket Support**: Real-time bidirectional communication
- **CORS Support**: Cross-origin resource sharing configuration
- **Middleware Support**: Request/response processing pipeline

### Database & Persistence
- **PostgreSQL Integration**: Async database operations with connection pooling
- **Repository Pattern**: Type-safe CRUD operations with Pydantic models
- **SQL Injection Protection**: Comprehensive input validation and sanitization
- **Transaction Support**: Context managers for database transactions
- **Table Creation**: Basic CREATE TABLE SQL generation (no migration tools)

### Security & Authentication
- **JWT Authentication**: Token-based authentication with role/permission support
- **Input Validation**: Comprehensive parameter validation throughout the framework
- **Secure Repository**: SQL injection protection with whitelisting
- **Correlation ID Tracking**: Request tracing across distributed services
- **Note**: Rate limiting is NOT implemented (planned for future release)

### Performance & Monitoring
- **Benchmarked Performance**: 1,800+ RPS throughput with 3.3ms average latency†
- **Connection Pooling**: Efficient database and messaging connection management
- **Batch Processing**: Bulk operations with configurable batch sizes
- **Resource Management**: Proper cleanup and lifecycle management
- **Structured Logging**: Correlation ID propagation and service-aware logging

†Performance measured in controlled load testing environment with local NATS server. See [load-testing/LOAD_TESTING_RESULTS.md](load-testing/LOAD_TESTING_RESULTS.md) for methodology.

### Development Tools
- **Load Testing**: Comprehensive performance testing framework using Locust
- **Debug Interface**: Secure backdoor for runtime service inspection (disabled by default)
- **Service Orchestration**: Multi-service coordination with auto-restart
- **Client Generation**: Automatic typed client generation for services
- **Comprehensive Examples**: Production-ready example applications

### Distributed Patterns
- **Saga Pattern**: Distributed transaction management with automatic compensation
- **Event Sourcing**: Basic event streaming capabilities via NATS
- **Circuit Breaker**: Available through retry decorators
- **Correlation Tracking**: Built-in request tracing across services

## 🛠️ Installation

### Prerequisites
- Python 3.11 or higher
- NATS server with JetStream enabled
- PostgreSQL (optional, for database features)

### For Framework Development

```bash
# Clone the repository
git clone https://github.com/sndwch/microservices.git
cd microservices

# Install with uv (recommended)
uv sync --extra dev

# Or with pip
pip install -e ".[dev]"
```

### For Using Cliffracer in Your Project

```bash
# Option 1: Install built package (recommended)
uv build  # Run this in the Cliffracer directory
pip install /path/to/cliffracer/dist/cliffracer-1.0.0-py3-none-any.whl

# Option 2: Development install (editable)
pip install -e /path/to/cliffracer

# Option 3: Add to your pyproject.toml
# [project]
# dependencies = ["cliffracer @ file:///path/to/cliffracer"]
```

📖 **See [INSTALL.md](INSTALL.md) for detailed installation guide for consuming projects.**

### Start Dependencies

```bash
# Start NATS server with JetStream
docker run -d --name nats-server \
  -p 4222:4222 -p 8222:8222 \
  nats:alpine -js -m 8222

# Start PostgreSQL (optional)
cd deployment/docker
docker-compose up postgres -d
```

## 🏃‍♂️ Quick Start

### 1. Basic Service

```python
from cliffracer import CliffracerService, ServiceConfig

# Create a simple service
config = ServiceConfig(
    name="user_service",
    nats_url="nats://localhost:4222"
)
service = CliffracerService(config)

@service.rpc
async def get_user(user_id: str) -> dict:
    """Get user by ID"""
    return {"user_id": user_id, "name": "John Doe"}

@service.event("user.created")
async def on_user_created(user_data: dict):
    """Handle user created events"""
    print(f"User created: {user_data}")

if __name__ == "__main__":
    service.run()
```

### 2. HTTP + NATS Service

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.mixins import HTTPMixin

class UserService(CliffracerService, HTTPMixin):
    def __init__(self):
        config = ServiceConfig(
            name="user_service",
            nats_url="nats://localhost:4222"
        )
        super().__init__(config)
        self._http_port = 8080

    @self.rpc
    @self.http.get("/users/{user_id}")
    async def get_user(self, user_id: str) -> dict:
        return {"user_id": user_id, "name": "John Doe"}

service = UserService()
service.run()
```

### 3. Database Integration

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer.database import SecureRepository
from cliffracer.database.models import User

class UserService(CliffracerService):
    def __init__(self):
        config = ServiceConfig(name="user_service")
        super().__init__(config)
        self.users = SecureRepository(User)

    @self.rpc
    async def create_user(self, name: str, email: str) -> dict:
        user = User(name=name, email=email)
        created_user = await self.users.create(user)
        return created_user.model_dump()

    @self.rpc
    async def get_user(self, user_id: str) -> dict:
        user = await self.users.get(user_id)
        return user.model_dump() if user else None
```

### 4. WebSocket Real-time Updates

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.mixins import WebSocketMixin

class NotificationService(CliffracerService, WebSocketMixin):
    def __init__(self):
        config = ServiceConfig(name="notification_service")
        super().__init__(config)
        self._websocket_port = 8081

    @self.event("user.activity")
    async def broadcast_activity(self, activity_data: dict):
        """Broadcast user activity to all WebSocket clients"""
        await self.websocket_broadcast(activity_data)

service = NotificationService()
service.run()
```

## 📖 Documentation

### Architecture Guides
- [Architecture Overview](docs/ARCHITECTURE.md) - System design and patterns
- [Saga Pattern](docs/patterns/saga.md) - Distributed transaction management
- [Debugging Guide](docs/debugging/backdoor.md) - Live service debugging

### Examples
- [Basic Services](examples/basic/) - Simple NATS services and patterns
- [E-commerce System](examples/ecommerce/) - Complete microservices system
- [WebSocket Services](examples/websocket/) - Real-time communication
- [Database Integration](examples/database/) - Repository pattern examples
- [Saga Pattern](examples/saga/) - Distributed transactions
- [All Examples](examples/) - Complete examples overview

### Guides
- [Installation Guide](INSTALL.md) - Detailed installation instructions
- [Quick Start](docs/getting-started/quickstart.md) - Get up and running
- [HTTP/REST Guide](docs/http-guide.md) - Building REST APIs
- [WebSocket Guide](docs/websocket-guide.md) - Real-time communication
- [API Reference](docs/api-reference.md) - Classes and methods reference
- [Known Limitations](KNOWN_LIMITATIONS.md) - What's not implemented
- [Security Checklist](SECURITY_CHECKLIST.md) - Pre-deployment security

## 🧪 Testing

Run the comprehensive test suite:

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/cliffracer --cov-report=html

# Run performance tests
uv run pytest tests/performance/

# Run integration tests
uv run pytest tests/integration/
```

## 🏗️ Project Structure

```
src/cliffracer/
├── core/                    # Core framework components
│   ├── base_service.py     # Base NATS service
│   ├── consolidated_service.py  # Main service class
│   ├── mixins.py           # Feature mixins (HTTP, WebSocket, etc.)
│   ├── decorators.py       # Service decorators (@rpc, @event, etc.)
│   ├── correlation.py      # Request correlation tracking
│   └── validation.py       # Input validation utilities
├── database/               # Database integration
│   ├── models.py          # Pydantic models
│   ├── repository.py      # Basic repository pattern
│   ├── secure_repository.py  # Security-enhanced repository
│   └── connection.py      # Connection management
├── auth/                  # Authentication & authorization
│   ├── simple_auth.py     # JWT-based authentication
│   ├── framework.py       # Auth framework integration
│   └── middleware.py      # FastAPI auth middleware
├── performance/           # Performance optimization
│   ├── batch_processor.py # Bulk operation processing
│   ├── connection_pool.py # Connection pooling
│   └── metrics.py         # Performance metrics
├── middleware/            # Request/response middleware
│   └── correlation.py     # Correlation ID middleware
├── logging/               # Structured logging
│   ├── correlation_logging.py  # Correlation-aware logging
│   └── config.py          # Logging configuration
├── patterns/              # Distributed patterns
│   └── saga.py            # Saga pattern implementation
└── debug/                 # Development tools
    ├── backdoor.py        # Secure debug interface
    └── inspector.py       # Service inspection tools
```

## 📋 Known Limitations & Roadmap

See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) for a comprehensive list of:
- Features that are NOT implemented (despite documentation mentions)
- Partially working features and their limitations
- Detailed roadmap with priorities
- Workarounds for missing features

**Key Limitations**:
- No rate limiting (use reverse proxy)
- No database migrations (use Alembic)
- Basic service discovery only
- Partial auth system (SimpleAuthService works, decorators broken)

## 🔧 Configuration

### Environment Variables

```bash
# NATS Configuration
NATS_URL=nats://localhost:4222
NATS_USER=optional_user
NATS_PASSWORD=optional_password

# Database Configuration (optional)
DB_HOST=localhost
DB_PORT=5432
DB_USER=cliffracer_user
DB_PASSWORD=cliffracer_password
DB_NAME=cliffracer

# Authentication (optional)
AUTH_SECRET_KEY=your-super-secret-key-here
AUTH_TOKEN_EXPIRY_HOURS=24

# Debug Features (optional)
BACKDOOR_ENABLED=false
BACKDOOR_PASSWORD=auto-generated-if-not-set
```

### Service Configuration

```python
from cliffracer import ServiceConfig

config = ServiceConfig(
    name="my_service",
    nats_url="nats://localhost:4222",
    enable_auth=True,
    enable_metrics=True,
    batch_size=100,
    connection_pool_size=10
)

service = CliffracerService(config)
```

## 🚀 Production Deployment

### Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY . /app

RUN pip install uv && uv sync --no-dev

CMD ["python", "-m", "your_service"]
```

### Kubernetes

See [deployment/kubernetes/](deployment/kubernetes/) for complete Kubernetes manifests.

### Performance Tuning

For production deployments:

```python
from cliffracer.core.mixins import PerformanceMixin

class ProductionService(CliffracerService, PerformanceMixin):
    def __init__(self):
        super().__init__(
            name="production_service",
            # Performance settings
            connection_pool_size=20,
            batch_size=500,
            enable_metrics=True,
            enable_correlation_tracking=True
        )
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed guidelines.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- Built on top of [NATS.io](https://nats.io/) for messaging
- Uses [FastAPI](https://fastapi.tiangolo.com/) for HTTP integration
- Database integration via [asyncpg](https://github.com/MagicStack/asyncpg)
- Testing with [pytest](https://pytest.org/)

## 📞 Support

- 📧 Email: support@cliffracer.dev
- 💬 Discord: [Cliffracer Community](https://discord.gg/cliffracer)
- 🐛 Issues: [GitHub Issues](https://github.com/sndwch/microservices/issues)
- 📖 Documentation: [docs.cliffracer.dev](https://docs.cliffracer.dev)

---

**Built with ❤️ for the Python microservices community**

