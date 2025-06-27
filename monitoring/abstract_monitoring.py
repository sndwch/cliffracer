"""
Abstract monitoring interface for different monitoring backends
Supports Zabbix, CloudWatch, Prometheus, Datadog, etc.
"""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MetricType(str, Enum):
    """Types of metrics"""

    COUNTER = "counter"  # Monotonically increasing
    GAUGE = "gauge"  # Point-in-time value
    HISTOGRAM = "histogram"  # Distribution of values
    SUMMARY = "summary"  # Summary statistics
    TIMER = "timer"  # Timing measurements


class AlertSeverity(str, Enum):
    """Alert severity levels"""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class MetricUnit(str, Enum):
    """Metric units"""

    COUNT = "count"
    PERCENT = "percent"
    SECONDS = "seconds"
    MILLISECONDS = "milliseconds"
    BYTES = "bytes"
    MEGABYTES = "megabytes"
    REQUESTS_PER_SECOND = "requests_per_second"
    ERRORS_PER_SECOND = "errors_per_second"


@dataclass
class Metric:
    """A single metric data point"""

    name: str
    value: int | float
    metric_type: MetricType
    unit: MetricUnit = MetricUnit.COUNT
    timestamp: datetime | None = None
    tags: dict[str, str] = field(default_factory=dict)
    dimensions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


@dataclass
class Alert:
    """Alert definition"""

    name: str
    description: str
    condition: str  # Condition expression
    severity: AlertSeverity
    threshold: int | float
    metric_name: str
    enabled: bool = True
    tags: dict[str, str] = field(default_factory=dict)

    # Notification settings
    notification_channels: list[str] = field(default_factory=list)
    repeat_interval_minutes: int = 60
    max_notifications: int = 10


@dataclass
class Dashboard:
    """Dashboard definition"""

    name: str
    description: str
    widgets: list[dict[str, Any]] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    auto_refresh_seconds: int = 60


@dataclass
class MonitoringConfig:
    """Configuration for monitoring backends"""

    backend: str
    connection_params: dict[str, Any]
    default_tags: dict[str, str] = field(default_factory=dict)
    metric_prefix: str = "cultku"
    batch_size: int = 100
    flush_interval_seconds: int = 30
    retention_days: int = 30


class MonitoringClient(ABC):
    """Abstract base class for monitoring clients"""

    def __init__(self, config: MonitoringConfig):
        self.config = config
        self._connected = False
        self._metric_buffer: list[Metric] = []
        self._flush_task: asyncio.Task | None = None

    @abstractmethod
    async def connect(self) -> None:
        """Connect to monitoring system"""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from monitoring system"""
        pass

    @abstractmethod
    async def send_metric(self, metric: Metric) -> None:
        """Send a single metric"""
        pass

    @abstractmethod
    async def send_metrics(self, metrics: list[Metric]) -> None:
        """Send multiple metrics"""
        pass

    @abstractmethod
    async def create_alert(self, alert: Alert) -> str:
        """Create an alert rule"""
        pass

    @abstractmethod
    async def update_alert(self, alert_id: str, alert: Alert) -> None:
        """Update an alert rule"""
        pass

    @abstractmethod
    async def delete_alert(self, alert_id: str) -> None:
        """Delete an alert rule"""
        pass

    @abstractmethod
    async def list_alerts(self) -> list[dict[str, Any]]:
        """List all alerts"""
        pass

    @abstractmethod
    async def create_dashboard(self, dashboard: Dashboard) -> str:
        """Create a dashboard"""
        pass

    @abstractmethod
    async def update_dashboard(self, dashboard_id: str, dashboard: Dashboard) -> None:
        """Update a dashboard"""
        pass

    @abstractmethod
    async def delete_dashboard(self, dashboard_id: str) -> None:
        """Delete a dashboard"""
        pass

    @abstractmethod
    async def query_metrics(
        self,
        metric_name: str,
        start_time: datetime,
        end_time: datetime,
        tags: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Query historical metrics"""
        pass

    @abstractmethod
    async def get_health(self) -> dict[str, Any]:
        """Get monitoring system health"""
        pass

    # Buffered metric sending

    async def record_metric(
        self,
        name: str,
        value: int | float,
        metric_type: MetricType = MetricType.GAUGE,
        unit: MetricUnit = MetricUnit.COUNT,
        tags: dict[str, str] | None = None,
    ) -> None:
        """Record a metric (buffered)"""
        metric = Metric(
            name=f"{self.config.metric_prefix}.{name}",
            value=value,
            metric_type=metric_type,
            unit=unit,
            tags={**self.config.default_tags, **(tags or {})},
        )

        self._metric_buffer.append(metric)

        if len(self._metric_buffer) >= self.config.batch_size:
            await self._flush_metrics()

    async def _flush_metrics(self) -> None:
        """Flush buffered metrics"""
        if not self._metric_buffer:
            return

        try:
            metrics_to_send = self._metric_buffer.copy()
            self._metric_buffer.clear()

            await self.send_metrics(metrics_to_send)

        except Exception as e:
            # Re-add metrics to buffer on failure
            self._metric_buffer.extend(metrics_to_send)
            raise e

    async def start_background_flush(self) -> None:
        """Start background metric flushing"""
        if self._flush_task:
            return

        async def flush_loop():
            while self._connected:
                try:
                    await asyncio.sleep(self.config.flush_interval_seconds)
                    await self._flush_metrics()
                except asyncio.CancelledError:
                    break
                except Exception:
                    # Log error but continue
                    pass

        self._flush_task = asyncio.create_task(flush_loop())

    async def stop_background_flush(self) -> None:
        """Stop background metric flushing"""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        # Flush remaining metrics
        await self._flush_metrics()

    @property
    def is_connected(self) -> bool:
        return self._connected


class AbstractMonitoringCollector(ABC):
    """Abstract collector for gathering system metrics"""

    def __init__(self, monitoring_client: MonitoringClient):
        self.client = monitoring_client
        self._collection_task: asyncio.Task | None = None
        self._collecting = False

    @abstractmethod
    async def collect_system_metrics(self) -> list[Metric]:
        """Collect system-level metrics"""
        pass

    @abstractmethod
    async def collect_application_metrics(self) -> list[Metric]:
        """Collect application-level metrics"""
        pass

    @abstractmethod
    async def collect_business_metrics(self) -> list[Metric]:
        """Collect business-level metrics"""
        pass

    async def start_collection(self, interval_seconds: int = 60) -> None:
        """Start metric collection"""
        if self._collecting:
            return

        self._collecting = True

        async def collection_loop():
            while self._collecting:
                try:
                    # Collect all types of metrics
                    all_metrics = []
                    all_metrics.extend(await self.collect_system_metrics())
                    all_metrics.extend(await self.collect_application_metrics())
                    all_metrics.extend(await self.collect_business_metrics())

                    # Send to monitoring system
                    if all_metrics:
                        await self.client.send_metrics(all_metrics)

                    await asyncio.sleep(interval_seconds)

                except asyncio.CancelledError:
                    break
                except Exception:
                    # Log error but continue collecting
                    await asyncio.sleep(interval_seconds)

        self._collection_task = asyncio.create_task(collection_loop())

    async def stop_collection(self) -> None:
        """Stop metric collection"""
        self._collecting = False
        if self._collection_task:
            self._collection_task.cancel()
            try:
                await self._collection_task
            except asyncio.CancelledError:
                pass


class MonitoringFactory:
    """Factory for creating monitoring clients"""

    _clients: dict[str, type] = {}

    @classmethod
    def register_client(cls, backend: str, client_class: type):
        """Register a monitoring client implementation"""
        cls._clients[backend] = client_class

    @classmethod
    def create_client(cls, config: MonitoringConfig) -> MonitoringClient:
        """Create a monitoring client instance"""
        if config.backend not in cls._clients:
            raise ValueError(f"Unknown monitoring backend: {config.backend}")

        return cls._clients[config.backend](config)

    @classmethod
    def list_backends(cls) -> list[str]:
        """List available monitoring backends"""
        return list(cls._clients.keys())


# Decorators for monitoring


def monitor_performance(metric_name: str = None):
    """Decorator to monitor method performance"""

    def decorator(func):
        import functools
        import time

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            success = True
            error = None

            try:
                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
                return result
            except Exception as e:
                success = False
                error = str(e)
                raise
            finally:
                execution_time = (time.time() - start_time) * 1000  # milliseconds

                # Get monitoring client from service if available
                if args and hasattr(args[0], "monitoring_client"):
                    client = args[0].monitoring_client
                    name = metric_name or f"{func.__name__}_execution_time"

                    await client.record_metric(
                        name=name,
                        value=execution_time,
                        metric_type=MetricType.TIMER,
                        unit=MetricUnit.MILLISECONDS,
                        tags={
                            "method": func.__name__,
                            "success": str(success),
                            "error": error or "none",
                        },
                    )

        return async_wrapper

    return decorator


def count_requests(metric_name: str = None):
    """Decorator to count method invocations"""

    def decorator(func):
        import functools

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Increment request counter
            if args and hasattr(args[0], "monitoring_client"):
                client = args[0].monitoring_client
                name = metric_name or f"{func.__name__}_requests"

                await client.record_metric(
                    name=name,
                    value=1,
                    metric_type=MetricType.COUNTER,
                    unit=MetricUnit.COUNT,
                    tags={"method": func.__name__},
                )

            return (
                await func(*args, **kwargs)
                if asyncio.iscoroutinefunction(func)
                else func(*args, **kwargs)
            )

        return async_wrapper

    return decorator


# Utility functions


def create_service_dashboard(service_name: str) -> Dashboard:
    """Create a standard service dashboard"""
    widgets = [
        {
            "type": "metric_chart",
            "title": f"{service_name} Request Rate",
            "metrics": [f"cultku.{service_name}.requests"],
            "chart_type": "line",
        },
        {
            "type": "metric_chart",
            "title": f"{service_name} Response Time",
            "metrics": [f"cultku.{service_name}.execution_time"],
            "chart_type": "line",
        },
        {
            "type": "metric_chart",
            "title": f"{service_name} Error Rate",
            "metrics": [f"cultku.{service_name}.errors"],
            "chart_type": "line",
        },
        {
            "type": "metric_chart",
            "title": f"{service_name} Memory Usage",
            "metrics": [f"cultku.{service_name}.memory_usage"],
            "chart_type": "area",
        },
    ]

    return Dashboard(
        name=f"{service_name} Service Dashboard",
        description=f"Monitoring dashboard for {service_name} service",
        widgets=widgets,
        tags={"service": service_name, "auto_generated": "true"},
    )


def create_service_alerts(service_name: str) -> list[Alert]:
    """Create standard service alerts"""
    alerts = [
        Alert(
            name=f"{service_name} High Error Rate",
            description=f"Error rate is above 5% for {service_name}",
            condition=f"cultku.{service_name}.error_rate > 5",
            severity=AlertSeverity.HIGH,
            threshold=5.0,
            metric_name=f"cultku.{service_name}.error_rate",
            tags={"service": service_name},
        ),
        Alert(
            name=f"{service_name} High Response Time",
            description=f"Response time is above 1000ms for {service_name}",
            condition=f"cultku.{service_name}.execution_time > 1000",
            severity=AlertSeverity.MEDIUM,
            threshold=1000.0,
            metric_name=f"cultku.{service_name}.execution_time",
            tags={"service": service_name},
        ),
        Alert(
            name=f"{service_name} Service Down",
            description=f"{service_name} service is not responding",
            condition=f"cultku.{service_name}.health == 0",
            severity=AlertSeverity.CRITICAL,
            threshold=0,
            metric_name=f"cultku.{service_name}.health",
            tags={"service": service_name},
        ),
    ]

    return alerts
