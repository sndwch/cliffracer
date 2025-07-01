# Known Limitations and Roadmap

This document provides a transparent overview of Cliffracer's current limitations and planned improvements.

## 🚫 Features NOT Implemented

Despite being mentioned in some documentation, these features are **NOT** available:

### 1. Rate Limiting
- **Status**: Not implemented
- **Workaround**: Use a reverse proxy (nginx, Traefik) or API gateway
- **Planned**: Future middleware implementation

### 2. Database Migrations
- **Status**: Only basic CREATE TABLE SQL generation
- **Workaround**: Use Alembic or similar migration tools
- **What Works**: Table creation SQL, but no versioning or rollback

### 3. Full Service Discovery
- **Status**: Only basic service info endpoint (`get_service_info`)
- **Missing**: Service registry, health checks, load balancing
- **Workaround**: Use Consul, etcd, or Kubernetes service discovery

### 4. Backend Switching
- **Status**: `MessagingFactory` has `NotImplementedError`
- **What Works**: Only NATS backend
- **AWS/Redis**: Client exists but not integrated with framework

## ⚠️ Partially Implemented Features

### 1. Authentication System
- **Working**: `SimpleAuthService` with JWT tokens
- **Broken**: Old `@requires_auth` decorators (import errors)
- **Missing**: OAuth2/OIDC support, session management

### 2. Monitoring Integration
- **Working**: Basic metrics collection, file export
- **False Claims**: No real Zabbix/Prometheus protocol support
- **Workaround**: Export metrics to files and use external collectors

### 3. AWS Integration
- **Status**: AWS client exists but not integrated
- **What Works**: Can use AWS client directly
- **What Doesn't**: No framework integration, no service decorators

## ✅ What IS Production-Ready

### Core Features
- NATS messaging with JetStream
- RPC and event-driven communication
- HTTP/REST APIs via FastAPI
- WebSocket support
- PostgreSQL integration
- SQL injection protection
- JWT authentication (SimpleAuthService)
- Correlation ID tracking
- Structured logging
- Performance optimization
- Saga pattern for distributed transactions

### Performance
- 1,800+ RPS throughput (benchmarked)
- 3.3ms average latency
- Connection pooling
- Batch processing
- Efficient resource management

### Security
- Comprehensive input validation
- Secure repository pattern
- Password hashing (PBKDF2)
- Secure debug interface
- Correlation tracking

## 🗺️ Roadmap

### High Priority
1. **Rate Limiting Middleware**
   - Token bucket algorithm
   - Per-IP and per-user limits
   - Redis backend for distributed rate limiting

2. **Database Migration Support**
   - Alembic integration
   - Automatic migration generation
   - Version tracking

3. **Enhanced Service Discovery**
   - Health check endpoints
   - Service registry with TTL
   - Load balancer integration

### Medium Priority
4. **Complete Auth Framework**
   - Fix decorator-based authentication
   - Add OAuth2/OIDC support
   - Session management

5. **Real Monitoring Integration**
   - OpenTelemetry support
   - Prometheus metrics endpoint
   - Distributed tracing

6. **Multi-Backend Messaging**
   - Complete AWS integration
   - Redis pub/sub support
   - Kafka adapter

### Low Priority
7. **Additional Patterns**
   - Circuit breaker implementation
   - Bulkhead pattern
   - Event sourcing helpers

8. **Developer Experience**
   - CLI improvements
   - Project scaffolding
   - VS Code extension

## 💡 Contributing

If you'd like to help implement any of these features:

1. Check the [GitHub Issues](https://github.com/sndwch/microservices/issues)
2. Read [CONTRIBUTING.md](CONTRIBUTING.md)
3. Join the discussion on Discord

## 📝 Notes

- This document is regularly updated as features are implemented
- Check the [CHANGELOG.md](CHANGELOG.md) for recent improvements
- The core NATS functionality is stable and production-ready
- Focus on using working features rather than waiting for missing ones