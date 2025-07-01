# Examples Directory

**⚠️ IMPORTANT: Read [../IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md) first to understand what examples actually work.**

This directory contains examples of working Cliffracer functionality. We've focused on examples that demonstrate the solid, production-ready core features.

## ✅ Working Examples

### [E-commerce System](ecommerce/)
**Complete working system** demonstrating:
- NATS-based microservices communication
- HTTP/REST APIs with FastAPI integration
- Service orchestration and lifecycle management
- Schema validation with Pydantic
- Structured logging
- Load testing framework

**Status**: ✅ Fully functional and tested
**Use case**: Production-ready microservices architecture

### [Database Integration](database/)
**PostgreSQL-powered services** demonstrating:
- Database connection management and pooling
- Repository pattern for clean data access
- Transaction support with context managers
- Database model validation with Pydantic
- Service integration with database persistence

**Status**: ✅ Fully functional and tested
**Use case**: Database-driven microservices

### [WebSocket Services](websocket/)
**Real-time communication** demonstrating:
- WebSocket connection management
- NATS to WebSocket message broadcasting
- Bidirectional real-time communication
- Automatic client connection tracking
- Integration test client included

**Status**: ✅ Fully functional and tested
**Use case**: Real-time applications and notifications

### [Load Testing](../load-testing/)
**Performance validation framework** with:
- Sub-millisecond latency validation
- Throughput testing
- Service reliability testing
- Comprehensive metrics collection

**Status**: ✅ Fully functional
**Use case**: Validating service performance

## ❌ Broken Examples (Avoid)

### [Auth Patterns](auth-patterns.md)
**Status**: ❌ Broken - Authentication system has import errors
**Issue**: `@require_auth` decorators don't work, auth framework disabled

### [Backend Migration](backend-migration.md) 
**Status**: ❌ Broken - Backend switching not implemented
**Issue**: `MessagingFactory` has `NotImplementedError`

### [Monitoring Setup](monitoring-setup.md)
**Status**: ❌ Misleading - Only basic file export works
**Issue**: Claims about Zabbix/Prometheus integration are false

### [AWS Examples](aws/)
**Status**: ⚠️ Partial - AWS client exists but not integrated
**Issue**: Not connected to core framework functionality

## 🚀 Getting Started

1. **Start with [E-commerce Example](ecommerce/)** - It's a complete working system
2. **Read the [Load Testing Guide](../load-testing/)** to understand performance
3. **Focus on NATS core features** which are production-ready
4. **Avoid broken examples** until they're fixed

## 📂 Directory Structure

```
examples/
├── ecommerce/          # ✅ Complete working e-commerce system
├── aws/               # ⚠️ Partial - AWS client only, not integrated
├── auth-patterns.md   # ❌ Broken - Auth system disabled
├── backend-migration.md # ❌ Broken - Not implemented
└── monitoring-setup.md  # ❌ Misleading - False integration claims
```

## 🛠️ What You Can Build Today

Based on the working examples:

- **High-performance NATS microservices** with RPC communication
- **HTTP APIs** backed by NATS messaging
- **WebSocket services** for real-time communication  
- **Service orchestration** with multiple coordinated services
- **Event-driven architectures** using NATS pub/sub
- **Load testing frameworks** for performance validation

## 🚫 What to Avoid Until Fixed

- Authentication and authorization systems
- Multi-backend messaging (AWS, etc.)
- Production monitoring integrations
- Complex broadcast patterns (some are broken)

---

**Focus on the working examples to build solid microservices on the proven NATS foundation.**