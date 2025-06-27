# Monitoring Setup Example

This example demonstrates how to set up comprehensive monitoring for NATS microservices using Zabbix, structured logging, and metrics collection.

## Overview

The monitoring setup includes:

- **Zabbix Server** for infrastructure and application monitoring
- **Metrics Collection Service** for custom business metrics
- **Structured Logging** with Loguru for centralized log analysis
- **NATS Monitoring** for message broker health
- **Health Checks** for service availability
- **Custom Dashboards** for business insights

## Quick Start

### 1. Start Complete Monitoring Stack

```bash
# Start everything with Docker Compose
docker-compose up -d

# Or use the monitoring profile
docker-compose --profile monitoring up -d
```

### 2. Access Monitoring Dashboards

- **Zabbix Web UI**: http://localhost:8080 (admin/zabbix)
- **NATS Monitoring**: http://localhost:8222
- **Service APIs**: http://localhost:8001/docs, http://localhost:8002/docs

## Monitoring Components

### Zabbix Server

Complete monitoring solution with:
- Infrastructure metrics (CPU, memory, disk, network)
- Application metrics (response times, error rates)
- Custom business metrics
- Alerting and notifications
- Historical data analysis

### Metrics Collection Service

**File**: `monitoring/metrics_service.py`

Collects and exports metrics from:
- NATS server statistics
- Service health checks
- Business metrics (orders, users, etc.)
- Custom application metrics

### Structured Logging

**File**: `logging_config.py`

Provides:
- JSON-structured logs
- Contextual logging with request IDs
- Automatic log rotation
- Integration with log aggregation systems

## Configuration Files

### Docker Compose Configuration

```yaml
# docker-compose.yml (monitoring section)
services:
  zabbix-server:
    image: zabbix/zabbix-server-pgsql:alpine-6.4-latest
    environment:
      - DB_SERVER_HOST=postgres
      - POSTGRES_USER=zabbix
      - POSTGRES_PASSWORD=zabbix
      - POSTGRES_DB=zabbix
    depends_on:
      - postgres
    ports:
      - "10051:10051"

  zabbix-web:
    image: zabbix/zabbix-web-nginx-pgsql:alpine-6.4-latest
    environment:
      - ZBX_SERVER_HOST=zabbix-server
      - DB_SERVER_HOST=postgres
      - POSTGRES_USER=zabbix
      - POSTGRES_PASSWORD=zabbix
      - POSTGRES_DB=zabbix
    ports:
      - "8080:8080"
    depends_on:
      - zabbix-server

  zabbix-agent:
    image: zabbix/zabbix-agent:alpine-6.4-latest
    environment:
      - ZBX_HOSTNAME=docker-host
      - ZBX_SERVER_HOST=zabbix-server
    privileged: true
    pid: "host"
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /dev:/host/dev:ro
```

### Zabbix Agent Configuration

```conf
# monitoring/zabbix/agent/nats_services.conf
UserParameter=nats.connections,curl -s http://localhost:8222/connz | jq '.num_connections'
UserParameter=nats.messages.in,curl -s http://localhost:8222/varz | jq '.in_msgs'
UserParameter=nats.messages.out,curl -s http://localhost:8222/varz | jq '.out_msgs'
UserParameter=service.health[*],curl -s http://localhost:$1/health | jq -r '.status'
UserParameter=service.rpc.count[*],curl -s http://localhost:$1/info | jq '.rpc_methods | length'
```

## Monitoring Features

### 1. Service Health Monitoring

```python
# Built into HTTPService
@self.app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": self.config.name,
        "nats_connected": self.nc and not self.nc.is_closed,
        "uptime": time.time() - self.start_time,
        "version": "1.0.0"
    }
```

### 2. Custom Metrics Collection

```python
# metrics_service.py
class MetricsService(Service):
    @rpc
    async def collect_metrics(self) -> dict:
        return {
            "nats_stats": await self.get_nats_stats(),
            "service_health": await self.check_services(),
            "business_metrics": await self.get_business_metrics(),
            "timestamp": datetime.utcnow().isoformat()
        }
    
    async def get_nats_stats(self):
        # Collect NATS server statistics
        async with aiohttp.ClientSession() as session:
            async with session.get('http://localhost:8222/varz') as resp:
                return await resp.json()
```

### 3. Structured Logging

```python
# logging_config.py
from loguru import logger

def configure_logging(service_name: str = None):
    logger.remove()  # Remove default handler
    
    # JSON format for production
    logger.add(
        f"logs/{service_name or 'app'}.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}",
        rotation="100 MB",
        retention="30 days",
        compression="gz",
        serialize=True  # JSON format
    )
    
    # Console output for development
    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="INFO"
    )

# Usage in services
logger.info("Service started", service=service_name, port=port)
logger.error("Failed to process request", error=str(e), request_id=request_id)
```

## Monitoring Dashboards

### Zabbix Templates

Pre-configured templates for:
- NATS server monitoring
- Microservice health checks
- Business metrics tracking
- Infrastructure monitoring

### Custom Dashboards

1. **Service Overview**
   - Service health status
   - Response times
   - Error rates
   - Request counts

2. **NATS Monitoring**
   - Connection counts
   - Message throughput
   - Subject statistics
   - JetStream metrics

3. **Business Metrics**
   - User registrations
   - Order processing
   - Revenue tracking
   - System utilization

## Alerting Configuration

### Service Health Alerts

```yaml
# Alert when service is down
- name: "Service Down"
  condition: "service.health[8001] != 'healthy'"
  severity: "High"
  action: "Email + SMS notification"

# Alert on high error rate
- name: "High Error Rate"
  condition: "error_rate > 5%"
  severity: "Warning"
  action: "Email notification"
```

### NATS Alerts

```yaml
# Alert on NATS connection issues
- name: "NATS Connection Loss"
  condition: "nats.connections = 0"
  severity: "Critical"
  action: "Immediate notification"

# Alert on message queue buildup
- name: "Message Backlog"
  condition: "nats.pending_messages > 1000"
  severity: "Warning"
  action: "Email notification"
```

## Log Analysis

### Structured Log Format

```json
{
  "timestamp": "2024-01-01T12:00:00.123Z",
  "level": "INFO",
  "service": "user_service",
  "method": "create_user",
  "request_id": "req_123456",
  "user_id": "user_789",
  "duration_ms": 150,
  "message": "User created successfully"
}
```

### Log Aggregation

```bash
# Query logs with jq
cat logs/user_service.log | jq 'select(.level == "ERROR")'
cat logs/*.log | jq 'select(.duration_ms > 1000)' | head -10

# Monitor logs in real-time
tail -f logs/user_service.log | jq .
```

## Performance Monitoring

### Response Time Tracking

```python
@rpc
@track_performance
async def create_user(self, request: CreateUserRequest):
    start_time = time.time()
    try:
        result = await self.process_user_creation(request)
        
        # Log performance metrics
        logger.info(
            "User creation completed",
            duration_ms=int((time.time() - start_time) * 1000),
            request_id=request.correlation_id
        )
        
        return result
    except Exception as e:
        logger.error(
            "User creation failed",
            error=str(e),
            duration_ms=int((time.time() - start_time) * 1000),
            request_id=request.correlation_id
        )
        raise
```

### Business Metrics

```python
class MetricsCollector:
    def __init__(self):
        self.metrics = {
            "users_created_today": 0,
            "orders_processed_today": 0,
            "revenue_today": 0.0,
            "active_sessions": 0
        }
    
    @event_handler("users.created")
    async def track_user_creation(self, user_id: str, **kwargs):
        self.metrics["users_created_today"] += 1
        
        # Send to Zabbix
        await self.send_metric("users.created.daily", self.metrics["users_created_today"])
    
    async def send_metric(self, key: str, value: any):
        # Send custom metric to Zabbix
        # Implementation depends on your Zabbix setup
        pass
```

## Deployment

### Production Deployment

```bash
# Deploy with monitoring
docker-compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d

# Scale services with monitoring
docker-compose up -d --scale user_service=3 --scale notification_service=2
```

### Environment Configuration

```bash
# .env for monitoring
ZABBIX_DB_PASSWORD=secure_password
LOG_LEVEL=INFO
METRICS_INTERVAL=30
ALERT_EMAIL=ops@company.com
SMTP_SERVER=smtp.company.com
```

## Maintenance

### Log Rotation

```bash
# Automatic log rotation with logrotate
/var/log/cliffracer/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    create 644 app app
    postrotate
        systemctl reload cliffracer-services
    endscript
}
```

### Database Maintenance

```sql
-- Clean up old Zabbix data (run monthly)
DELETE FROM history WHERE clock < UNIX_TIMESTAMP(NOW() - INTERVAL 90 DAY);
DELETE FROM trends WHERE clock < UNIX_TIMESTAMP(NOW() - INTERVAL 1 YEAR);
```

## Troubleshooting

### Common Issues

1. **Zabbix Agent Not Connecting**
   ```bash
   # Check agent status
   docker logs zabbix-agent
   
   # Test agent connectivity
   zabbix_get -s localhost -k system.uptime
   ```

2. **Missing Metrics**
   ```bash
   # Check service health endpoints
   curl http://localhost:8001/health
   curl http://localhost:8001/info
   
   # Check NATS monitoring
   curl http://localhost:8222/varz
   ```

3. **Log Issues**
   ```bash
   # Check log permissions
   ls -la logs/
   
   # Test log rotation
   logrotate -d /etc/logrotate.d/cliffracer
   ```

## Next Steps

1. **[Alerting Setup](alerting-setup.md)** - Configure notifications (coming soon)
2. **[Performance Tuning](performance-tuning.md)** - Optimize monitoring overhead (coming soon)
3. **[Custom Metrics](custom-metrics.md)** - Add business-specific metrics (coming soon)
4. **[Log Analysis](log-analysis.md)** - Advanced log analysis techniques (coming soon)

---

**Note**: This is a placeholder document. The actual monitoring implementation is available in the `monitoring/` directory and `docker-compose.yml` file. Run `docker-compose up -d` to see the complete monitoring stack in action.
