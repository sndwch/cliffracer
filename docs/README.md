# Cliffracer Documentation

**⚠️ IMPORTANT: This framework is under active development. Many advanced features are not yet implemented.**

## 📖 Available Documentation

### Essential Reading
- **[../IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md)**: **READ THIS FIRST** - What actually works vs. what's broken
- **[Installation Guide](getting-started/installation.md)**: Basic setup instructions
- **[Debugging Guide](debugging/README.md)**: Backdoor debugging system

### Working Examples
- **[Basic Examples](../examples/README.md)**: Simple working patterns
- **[E-commerce Example](../examples/ecommerce/README.md)**: Complete working system
- **[Load Testing](../load-testing/README.md)**: Performance testing framework

## 🟢 What Actually Works

The core NATS functionality is solid and production-ready:
- NATS-based microservices with RPC communication
- HTTP/REST API integration using FastAPI  
- WebSocket support for real-time communication
- Service orchestration with auto-restart
- Schema validation using Pydantic models
- Structured logging with contextual information
- Load testing framework for performance validation

## 🔴 What Doesn't Work Yet

Many advanced features shown in old documentation are broken:
- Authentication/Authorization system (import errors)
- AWS messaging backend (not integrated)
- Real monitoring integration (only basic file export)
- Zabbix/Prometheus integration (false claims)
- Backend switching (NotImplementedError)

## 🚀 Quick Start

1. **Read IMPLEMENTATION_STATUS.md** to understand what works
2. **Follow the [Installation Guide](getting-started/installation.md)**
3. **Try the [E-commerce Example](../examples/ecommerce/README.md)**
4. **Build on the working NATS core functionality**

## 🛠️ Development Status

This framework aims to be a comprehensive microservices platform, but we're being honest about current limitations. The NATS messaging foundation is solid and ready for production use.

Focus on:
- NATS microservices
- HTTP API integration  
- WebSocket real-time features
- Service orchestration

Avoid until fixed:
- Authentication systems
- AWS integration
- Production monitoring claims
- Backend switching

---

**Remember**: Check [IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md) for the current truth about feature availability.