# Cliffracer

A NATS-based microservices framework for Python with HTTP and WebSocket integration.

> **⚠️ IMPORTANT: READ [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) BEFORE USING**
> 
> This framework is under active development. Many features shown in examples are not yet implemented or are broken. See the implementation status document for what actually works vs. what's planned.

## 🟢 What Actually Works (Production-Ready)

- **NATS-based microservices** with RPC communication
- **HTTP/REST API integration** using FastAPI
- **WebSocket support** for real-time communication
- **Service orchestration** with auto-restart capabilities
- **Schema validation** using Pydantic models
- **Structured logging** with service-specific loggers
- **Load testing framework** for performance validation

## 🔴 What Doesn't Work (Yet)

- **Authentication/Authorization**: Import errors, completely broken
- **AWS messaging backend**: Not integrated with core framework
- **Real monitoring integration**: Only basic file export works
- **Backend switching**: MessagingFactory has NotImplementedError
- **Zabbix/Prometheus integration**: Claims are false

## 🚀 Quick Start (What Actually Works)

### 1. Installation

```bash
# Clone repository
git clone https://github.com/sndwch/microservices.git
cd microservices

# Install with uv
uv sync --extra dev
```

### 2. Start NATS Server

```bash
# Start NATS server with JetStream
docker run -d --name nats-server -p 4222:4222 -p 8222:8222 nats:alpine -js -m 8222
```

### 3. Working Example

```python
from cliffracer import NATSService, HTTPNATSService, ServiceConfig
from pydantic import BaseModel

class UserRequest(BaseModel):
    username: str
    email: str

class UserService(HTTPNATSService):
    def __init__(self):
        config = ServiceConfig(name="user_service")
        super().__init__(config, port=8001)
    
    @self.post("/users")
    async def create_user(self, request: UserRequest):
        # This actually works
        return {"user_id": f"user_{request.username}"}

# This works
service = UserService()
service.run()
```

### 4. Test It

```bash
# Run the service
uv run python your_service.py

# Test the endpoint
curl -X POST "http://localhost:8001/users" \
  -H "Content-Type: application/json" \
  -d '{"username": "test", "email": "test@example.com"}'
```

## 📊 Feature Matrix (Honest Assessment)

| Feature | Status | Production Ready | Notes |
|---------|--------|------------------|-------|
| NATS Services | ✅ Complete | ✅ Yes | Core functionality works well |
| HTTP Integration | ✅ Complete | ✅ Yes | FastAPI integration functional |
| WebSocket Support | ✅ Complete | ✅ Yes | Real-time communication works |
| Service Orchestration | ✅ Complete | ✅ Yes | Auto-restart and lifecycle management |
| Schema Validation | ✅ Complete | ✅ Yes | Pydantic integration works |
| Load Testing | ✅ Complete | ✅ Yes | Comprehensive testing framework |
| Authentication | ❌ Broken | ❌ No | Import errors, disabled in exports |
| AWS Integration | ⚠️ Partial | ❌ No | Client exists but not integrated |
| Monitoring | ⚠️ Basic | ❌ No | File export only |
| Backend Switching | ❌ Broken | ❌ No | Factory not implemented |

## 🧪 Testing

```bash
# Run tests (this works)
uv run pytest

# Run linting (this works)
uv run ruff check .

# Check implementation status
cat IMPLEMENTATION_STATUS.md
```

## 📁 Working Examples

```
examples/
├── ecommerce/           # ✅ Complete working example
├── basic/              # ✅ Simple working patterns
└── load-testing/       # ✅ Performance testing
```

## 🚫 Examples to Avoid (Broken)

```
examples/
├── auth-patterns.md         # ❌ Auth system broken
├── backend-migration.md     # ❌ Backend switching broken
├── monitoring-setup.md      # ❌ Monitoring integration broken
└── docs/monitoring/         # ❌ False claims about Zabbix
```

## 🛠️ Development Status

### What We're Building
This framework aims to provide a comprehensive microservices platform with NATS messaging, but we're being honest about what's ready for production use.

### Core Philosophy
- **NATS-first**: Built around NATS messaging patterns
- **Type-safe**: Pydantic schema validation throughout
- **Developer-friendly**: Decorator-based service definitions
- **Production-ready core**: The NATS/HTTP/WebSocket foundation is solid

### What You Can Build Today
- High-performance NATS microservices
- HTTP APIs backed by NATS messaging
- Real-time WebSocket applications
- Service mesh architectures
- Event-driven systems

### What You Should Wait For
- Authentication systems
- Multi-backend messaging
- Production monitoring
- AWS integration

## 📚 Documentation

- **[Implementation Status](IMPLEMENTATION_STATUS.md)**: What works vs. what doesn't
- **[Getting Started](docs/getting-started/installation.md)**: Basic setup
- **Working Examples**: See `examples/ecommerce/` for a complete system

## ⚠️ Important Notes

1. **Read IMPLEMENTATION_STATUS.md first** - It contains the truth about what works
2. **Test everything** - Don't assume features work based on examples
3. **Focus on NATS core** - The messaging foundation is solid
4. **Avoid auth/AWS/monitoring** - These modules are broken or incomplete

## 🤝 Contributing

1. Fork the repository
2. Read IMPLEMENTATION_STATUS.md to understand what needs work
3. Focus on completing broken features or improving working ones
4. Add tests for any new functionality
5. Update implementation status when features are completed

## 📄 License

MIT License - see [LICENSE](LICENSE) file for details.

## 🆘 Support

- **Implementation Questions**: Check [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)
- **Issues**: [GitHub Issues](https://github.com/sndwch/microservices/issues)
- **Working Examples**: See `examples/ecommerce/` directory

---

**Remember**: This framework's core NATS functionality is production-ready and powerful. The honest assessment helps you build on solid foundations rather than broken promises.