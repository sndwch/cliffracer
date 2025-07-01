# ⚠️ BROKEN: Monitoring Setup Guide

> **THIS DOCUMENTATION IS BROKEN**
> 
> The monitoring integrations described here are not properly implemented. 
> See [IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md) for current status.

## Current Status: PARTIALLY IMPLEMENTED

The monitoring setup described in this document has the following issues:

1. **Zabbix integration fake**: Only writes to files, not real Zabbix protocol
2. **CloudWatch not integrated**: Client exists but not connected to services
3. **Prometheus claims false**: No Prometheus support implemented
4. **Dashboard automation broken**: No automatic dashboard creation

## What Actually Works

- **Basic metrics export**: File-based metrics collection works
- **Service health checks**: Basic health endpoints functional
- **Load testing metrics**: Performance testing framework works

## What Doesn't Work

- **Real Zabbix integration**: Claims of dashboards are false
- **CloudWatch integration**: Not connected to main framework
- **Prometheus metrics**: Not implemented despite claims
- **APM integration**: No application performance monitoring

## What Actually Works

The framework provides these monitoring capabilities:

### 1. Performance Metrics Collection
```python
from cliffracer.performance import PerformanceMetrics

# Automatic metrics collection for:
- Request count
- Response times
- Error rates
- Throughput
```

### 2. Structured Logging with Correlation
```python
from cliffracer import CliffracerService

# All services have correlation-aware logging
# Logs include correlation IDs for distributed tracing
```

### 3. Service Health Endpoints
```python
@service.rpc
async def get_service_info() -> dict:
    # Built-in service info endpoint
    return service.get_service_stats()
```

## What to Use Instead

For production monitoring, consider:
- External monitoring tools (Datadog, New Relic)
- Direct Prometheus client integration
- Custom metrics collection outside Cliffracer
- Standard FastAPI monitoring middleware

This document will be updated when proper monitoring integration is implemented.