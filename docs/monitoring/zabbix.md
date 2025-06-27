# Zabbix Integration

The framework includes comprehensive Zabbix monitoring integration for production-ready observability of your NATS microservices.

## Overview

Our Zabbix integration provides:

- **Automated Discovery**: Services are automatically discovered and monitored
- **Pre-built Templates**: Ready-to-use monitoring templates for NATS services
- **Custom Metrics**: Application-specific metrics collection
- **Alerting**: Production-ready alerts for service health and performance
- **Dashboards**: Visual monitoring dashboards

## Quick Setup

### 1. Start Monitoring Stack

```bash
# Start full monitoring stack with Docker Compose
docker-compose -f docker-compose-monitoring.yml up -d

# Check services
docker-compose -f docker-compose-monitoring.yml ps
```

This starts:

- **Zabbix Server**: Core monitoring engine
- **Zabbix Web UI**: Dashboard and configuration interface  
- **Zabbix Agent**: Metrics collection agent
- **PostgreSQL**: Zabbix database
- **Metrics Exporter**: Custom metrics collection service

### 2. Access Zabbix

Open [http://localhost:8080](http://localhost:8080) and login:

- **Username**: `admin`
- **Password**: `zabbix`

### 3. Import Templates

The NATS services template is automatically available at startup. You can also manually import:

1. Go to **Configuration** → **Templates**
2. Click **Import**
3. Upload `monitoring/zabbix/templates/nats_services_template.xml`
4. Click **Import**

## Monitoring Components

### NATS Server Monitoring

The framework monitors core NATS server metrics:

```yaml
Metrics Collected:
  - Connection count
  - Message throughput (in/out per second)
  - Data throughput (bytes in/out per second)
  - Memory usage
  - CPU usage
  - Uptime
  - Cluster status (if clustered)
```

#### NATS Server Dashboard

```bash
# View real-time NATS metrics
curl http://localhost:8222/varz | jq

# Connection information
curl http://localhost:8222/connz | jq

# Subject statistics
curl http://localhost:8222/subsz | jq
```

### Service Health Monitoring

Each microservice exposes health and metrics endpoints:

```python
# Automatic health endpoint for all services
GET /health
{
    "status": "healthy",
    "service": "user_service", 
    "nats_connected": true,
    "timestamp": "2024-01-01T12:00:00Z"
}

# Service information endpoint
GET /info
{
    "service": "user_service",
    "rpc_methods": ["create_user", "get_user"],
    "event_handlers": ["user_created"],
    "broadcast_methods": ["broadcast_user_created"]
}
```

#### Service Metrics Collected

- **Health Status**: Service availability and responsiveness
- **NATS Connection**: Connection status to message broker
- **Response Time**: API endpoint response times
- **Error Rate**: Rate of failed requests
- **RPC Method Count**: Number of available RPC methods
- **Event Handler Count**: Number of registered event handlers

### Container Monitoring

Docker container metrics are automatically collected:

- **CPU Usage**: Container CPU utilization percentage
- **Memory Usage**: RAM consumption
- **Restart Count**: Number of container restarts
- **Network I/O**: Network traffic statistics
- **Disk I/O**: Disk read/write operations

### Custom Application Metrics

Services can expose custom metrics via the metrics exporter:

```python
from monitoring.metrics_service import MetricsCollector

class MyService(ExtendedService):
    def __init__(self, config):
        super().__init__(config)
        self.custom_metrics = {
            "orders_processed": 0,
            "revenue_total": 0.0,
            "active_users": 0
        }
    
    @rpc
    async def process_order(self, order_data):
        # Business logic
        result = await self.handle_order(order_data)
        
        # Update custom metrics
        self.custom_metrics["orders_processed"] += 1
        self.custom_metrics["revenue_total"] += order_data["amount"]
        
        return result
    
    @rpc
    async def get_custom_metrics(self):
        """Expose custom metrics for collection"""
        return self.custom_metrics
```

## Alerting Configuration

### Pre-configured Alerts

The framework includes alerts for common issues:

#### Service Alerts

- **Service Down**: Service health check fails
- **NATS Disconnected**: Service loses connection to NATS
- **High Response Time**: API response time > 5 seconds
- **High Error Rate**: Error rate > 5% over 5 minutes

#### Infrastructure Alerts

- **High CPU Usage**: Container CPU > 80%
- **High Memory Usage**: Container memory > 90% 
- **Container Restart**: Container restarts detected
- **NATS Server Down**: NATS server unavailable

#### Custom Alert Example

```xml
<!-- Zabbix trigger configuration -->
<trigger>
    <expression>last(/NATS Microservices/service.user_service.response_time) > 5000</expression>
    <name>User Service High Response Time</name>
    <priority>HIGH</priority>
    <description>User service response time is above 5 seconds</description>
    <manual_close>YES</manual_close>
</trigger>
```

### Alert Channels

Configure notification channels in Zabbix:

#### Email Notifications

1. Go to **Administration** → **Media Types**
2. Configure **Email** media type
3. Set SMTP server details
4. Create user media for notifications

#### Slack Integration

```bash
# Configure Slack webhook in Zabbix
Media Type: Webhook
Script: slack_webhook.py
Parameters:
  - webhook_url: your-slack-webhook-url
  - channel: #alerts
  - username: zabbix-bot
```

#### PagerDuty Integration

```python
# Custom script for PagerDuty integration
import requests

def send_pagerduty_alert(routing_key, event_action, summary, severity):
    payload = {
        "routing_key": routing_key,
        "event_action": event_action,
        "payload": {
            "summary": summary,
            "severity": severity,
            "source": "zabbix-nats-monitoring"
        }
    }
    
    response = requests.post(
        "https://events.pagerduty.com/v2/enqueue",
        json=payload
    )
    return response.status_code == 202
```

## Dashboard Configuration

### Service Overview Dashboard

Create a dashboard showing all services:

```json
{
    "dashboard": {
        "title": "NATS Microservices Overview",
        "widgets": [
            {
                "type": "graph",
                "title": "Service Health Status",
                "items": [
                    "nats.service.status[user_service,8001]",
                    "nats.service.status[notification_service,8002]",
                    "nats.service.status[analytics_service,8003]"
                ]
            },
            {
                "type": "graph", 
                "title": "NATS Message Throughput",
                "items": [
                    "nats.server.messages.in",
                    "nats.server.messages.out"
                ]
            },
            {
                "type": "table",
                "title": "Container Resource Usage", 
                "items": [
                    "container.cpu[user_service]",
                    "container.memory[user_service]",
                    "container.cpu[notification_service]",
                    "container.memory[notification_service]"
                ]
            }
        ]
    }
}
```

### Service-Specific Dashboard

```json
{
    "dashboard": {
        "title": "User Service Monitoring",
        "widgets": [
            {
                "type": "gauge",
                "title": "Response Time",
                "item": "service.user_service.response_time",
                "min": 0,
                "max": 5000,
                "units": "ms"
            },
            {
                "type": "graph",
                "title": "Request Rate",
                "items": [
                    "service.user_service.requests_per_second",
                    "service.user_service.errors_per_second"
                ]
            },
            {
                "type": "table",
                "title": "Business Metrics",
                "items": [
                    "service.user_service.users_created_today",
                    "service.user_service.active_sessions"
                ]
            }
        ]
    }
}
```

## Advanced Monitoring

### Service Discovery

The framework includes automatic service discovery:

```python
# monitoring/service_discovery.py
import json
import asyncio
from nats_service import Service, ServiceConfig

class ServiceDiscovery:
    """Discovers running services for Zabbix monitoring"""
    
    async def discover_services(self):
        """Discover all running NATS services"""
        services = []
        
        # Query NATS for active services
        # Implementation depends on your service registry
        
        discovery_data = {
            "data": [
                {
                    "{#SERVICE.NAME}": "user_service",
                    "{#SERVICE.PORT}": "8001",
                    "{#SERVICE.TYPE}": "http"
                },
                {
                    "{#SERVICE.NAME}": "notification_service", 
                    "{#SERVICE.PORT}": "8002",
                    "{#SERVICE.TYPE}": "websocket"
                }
            ]
        }
        
        return json.dumps(discovery_data)
```

### Custom Metrics Collection

```python
# Enhanced metrics collection
class CustomMetricsExporter(MetricsExporterService):
    """Extended metrics exporter with custom business metrics"""
    
    async def collect_business_metrics(self):
        """Collect business-specific metrics"""
        metrics = {}
        
        # Collect from each service
        for service_name in self.monitored_services:
            try:
                business_metrics = await self.call_rpc(
                    service_name,
                    "get_business_metrics"
                )
                metrics[service_name] = business_metrics
                
            except Exception as e:
                self.logger.warning(f"Failed to collect metrics from {service_name}: {e}")
        
        return metrics
    
    async def export_to_zabbix(self, metrics):
        """Export metrics to Zabbix with proper formatting"""
        zabbix_items = []
        
        for service, service_metrics in metrics.items():
            for metric_name, value in service_metrics.items():
                zabbix_items.append({
                    "key": f"business.{service}.{metric_name}",
                    "value": value,
                    "timestamp": datetime.utcnow().timestamp()
                })
        
        await self.zabbix_sender.send_metrics("NATS-Services-Host", zabbix_items)
```

### Historical Data and Trends

Zabbix automatically stores historical data. Configure retention policies:

```sql
-- Configure data retention in Zabbix database
UPDATE config SET value = '365d' WHERE name = 'hk_history_global';
UPDATE config SET value = '90d' WHERE name = 'hk_trends_global';
```

### Performance Optimization

For high-throughput environments:

```yaml
# Zabbix server configuration optimization
zabbix_server.conf:
  StartPollers: 50
  StartPollersUnreachable: 10
  StartTrappers: 20
  StartPingers: 10
  StartDiscoverers: 5
  CacheSize: 512M
  HistoryCacheSize: 256M
  TrendCacheSize: 128M
  ValueCacheSize: 256M
```

## Troubleshooting

### Common Issues

#### Zabbix Agent Not Connecting

```bash
# Check agent configuration
docker exec zabbix-agent cat /etc/zabbix/zabbix_agentd.conf

# Check connectivity
docker exec zabbix-agent zabbix_get -s localhost -k system.uptime

# View agent logs
docker logs zabbix-agent
```

#### Missing Metrics

```bash
# Test custom user parameters
docker exec zabbix-agent zabbix_get -s localhost -k "nats.server.connections"

# Check NATS connectivity from agent
docker exec zabbix-agent curl http://nats:8222/varz
```

#### Performance Issues

```bash
# Check Zabbix server performance
docker exec zabbix-server zabbix_server -R config_cache_stats

# Monitor database performance
docker exec zabbix-postgres psql -U zabbix -c "
SELECT schemaname, tablename, seq_scan, seq_tup_read, idx_scan, idx_tup_fetch 
FROM pg_stat_user_tables 
ORDER BY seq_tup_read DESC;
"
```

### Debugging Metrics Collection

```python
# Debug metrics collection
import asyncio
from monitoring.metrics_service import MetricsCollector

async def debug_metrics():
    collector = MetricsCollector()
    await collector.start()
    
    # Test service metrics
    metrics = await collector.collect_service_metrics("user_service", 8001)
    print(f"Service metrics: {metrics}")
    
    # Test NATS metrics
    nats_metrics = await collector.collect_nats_metrics()
    print(f"NATS metrics: {nats_metrics}")
    
    await collector.stop()

if __name__ == "__main__":
    asyncio.run(debug_metrics())
```

## Next Steps

1. **[Metrics Collection](metrics.md)**: Deep dive into custom metrics
2. **[Dashboards](dashboards.md)**: Creating advanced dashboards  
3. **[Production Setup](../deployment/production.md)**: Production monitoring configuration
4. **[Log Integration](../logging/structured-logging.md)**: Correlate logs with metrics