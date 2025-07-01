# Cliffracer Documentation

**🚀 PRODUCTION READY: This framework is now fully functional and ready for production use.**

## 📖 Available Documentation

### Essential Reading
- **[../README.md](../README.md)**: Complete feature overview and quick start guide
- **[ARCHITECTURE.md](ARCHITECTURE.md)**: Comprehensive architecture and design guide
- **[Installation Guide](getting-started/installation.md)**: Setup and configuration
- **[../CLAUDE.md](../CLAUDE.md)**: Development guide for contributors

### Working Examples
- **[Basic Examples](../examples/README.md)**: Simple service patterns
- **[E-commerce Example](../examples/ecommerce/README.md)**: Complete microservices system
- **[Load Testing](../load-testing/README.md)**: Performance testing framework

### API Reference
- **[Service Classes](api/core.md)**: Core service functionality
- **[Database Layer](api/database.md)**: Repository patterns and models
- **[Authentication](api/auth.md)**: Security and authorization
- **[Performance](api/performance.md)**: Optimization features

## 🟢 Production-Ready Features

All core functionality is implemented and tested:

### Core Framework
- **NATS Microservices**: High-performance messaging with RPC and events
- **HTTP/REST APIs**: FastAPI integration with automatic documentation
- **WebSocket Support**: Real-time bidirectional communication
- **Database Integration**: PostgreSQL with connection pooling and repository pattern
- **Service Orchestration**: Multi-service coordination with auto-restart

### Security & Authentication
- **JWT Authentication**: Complete working authentication system
- **Role-based Authorization**: Permission and role management
- **SQL Injection Protection**: Comprehensive input validation and sanitization
- **Secure Debug Interface**: Authenticated backdoor with rate limiting
- **Correlation Tracking**: Distributed request tracing

### Performance & Monitoring
- **High Performance**: 1,800+ RPS with 3.3ms average latency
- **Connection Pooling**: Database and messaging optimization
- **Batch Processing**: Bulk operations with configurable sizes
- **Resource Management**: Proper async task cleanup
- **Structured Logging**: Correlation-aware logging throughout

### Development Tools
- **Load Testing Framework**: Comprehensive performance validation
- **Client Generation**: Automatic typed client generation
- **Timer System**: Scheduled task decorators
- **Comprehensive Examples**: Production-ready sample applications

## 🚀 Quick Start

1. **Follow the [Installation Guide](getting-started/installation.md)**
2. **Read the [Architecture Guide](ARCHITECTURE.md)** for design patterns
3. **Try the [E-commerce Example](../examples/ecommerce/README.md)**
4. **Build production services** using the framework

## 🏗️ Architecture Overview

```
Cliffracer Framework
├── Core Services (NATS-based messaging)
├── Security Layer (JWT auth, validation, SQL injection protection)
├── Performance Layer (connection pooling, batch processing)
├── Web Integration (HTTP/REST, WebSocket, CORS)
├── Database Layer (PostgreSQL, repository pattern, models)
├── Observability (correlation tracking, structured logging)
└── Development Tools (testing, debugging, client generation)
```

## 🛠️ Development Workflow

### Service Development
1. **Create Service**: Use `CliffracerService` with appropriate mixins
2. **Add Handlers**: Implement RPC and event handlers
3. **Configure Security**: Set up authentication and validation
4. **Add Database Models**: Define Pydantic models and repositories
5. **Write Tests**: Unit, integration, and performance tests
6. **Deploy**: Docker containers with health checks

### Testing Strategy
- **Unit Tests**: Individual component testing
- **Integration Tests**: Service communication validation
- **Performance Tests**: Load testing and benchmarks
- **Security Tests**: Authentication and authorization validation

### Configuration Management
```python
from cliffracer import ServiceConfig, CliffracerService

config = ServiceConfig(
    name="my_service",
    nats_url="nats://localhost:4222",
    enable_auth=True,
    enable_metrics=True
)

service = CliffracerService(config)
```

## 📚 Learn More

- **[Architecture Guide](ARCHITECTURE.md)**: Deep dive into design decisions
- **[Contributing Guide](../CONTRIBUTING.md)**: How to contribute to the project
- **[Changelog](../CHANGELOG.md)**: Version history and feature evolution
- **[Examples](../examples/)**: Comprehensive example applications

## 🎯 Use Cases

Perfect for:
- **Microservices Architectures**: Distributed system communication
- **Real-time Applications**: WebSocket and event-driven systems
- **API Backends**: REST APIs with database integration
- **Event Processing**: Publish/subscribe message patterns
- **High-Performance Services**: Sub-5ms response times

## 📞 Support

- 📧 Email: support@cliffracer.dev
- 🐛 Issues: [GitHub Issues](https://github.com/sndwch/microservices/issues)
- 📖 Documentation: [Complete Framework Guide](../README.md)

---

**Built for production. Ready for scale. Perfect for Python microservices.**