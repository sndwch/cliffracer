"""
CloudWatch monitoring implementation for AWS-native monitoring
Supports CloudWatch Metrics, Alarms, and Dashboards
"""

import json
import logging
from datetime import datetime
from typing import Any

import boto3

from .abstract_monitoring import (
    MonitoringClient,
    AbstractMonitoringCollector,
    Alert,
    Dashboard,
    Metric,
    MetricType,
    MetricUnit,
    MonitoringConfig,
    MonitoringFactory,
)


class CloudWatchClient(MonitoringClient):
    """CloudWatch implementation of monitoring client"""

    def __init__(self, config: MonitoringConfig):
        super().__init__(config)

        self.region = config.connection_params.get("region", "us-east-1")
        self.endpoint_url = config.connection_params.get("endpoint_url")  # For LocalStack
        self.namespace = config.connection_params.get("namespace", "Cultku/Services")

        # AWS clients
        self.cloudwatch = None
        self.logs = None

        self.logger = logging.getLogger("cloudwatch_monitoring")

    async def connect(self) -> None:
        """Connect to CloudWatch"""
        if self._connected:
            return

        try:
            # Initialize CloudWatch clients
            session = boto3.Session(region_name=self.region)

            client_kwargs = {}
            if self.endpoint_url:
                client_kwargs["endpoint_url"] = self.endpoint_url

            self.cloudwatch = session.client("cloudwatch", **client_kwargs)
            self.logs = session.client("logs", **client_kwargs)

            # Test connectivity
            self.cloudwatch.list_metrics(MaxRecords=1)

            self._connected = True
            await self.start_background_flush()

            self.logger.info(f"Connected to CloudWatch (region: {self.region})")

        except Exception as e:
            self.logger.error(f"Failed to connect to CloudWatch: {e}")
            raise

    async def disconnect(self) -> None:
        """Disconnect from CloudWatch"""
        if not self._connected:
            return

        await self.stop_background_flush()
        self._connected = False
        self.logger.info("Disconnected from CloudWatch")

    async def send_metric(self, metric: Metric) -> None:
        """Send a single metric to CloudWatch"""
        await self.send_metrics([metric])

    async def send_metrics(self, metrics: list[Metric]) -> None:
        """Send multiple metrics to CloudWatch"""
        if not self._connected:
            raise RuntimeError("Not connected to CloudWatch")

        if not metrics:
            return

        try:
            # Convert metrics to CloudWatch format
            metric_data = []

            for metric in metrics:
                # Prepare dimensions
                dimensions = []

                # Add tags as dimensions
                for key, value in metric.tags.items():
                    dimensions.append({"Name": key, "Value": str(value)})

                # Add explicit dimensions
                for key, value in metric.dimensions.items():
                    dimensions.append({"Name": key, "Value": str(value)})

                # CloudWatch metric data
                metric_datum = {
                    "MetricName": metric.name,
                    "Value": float(metric.value),
                    "Unit": self._map_unit_to_cloudwatch(metric.unit),
                    "Timestamp": metric.timestamp,
                }

                if dimensions:
                    metric_datum["Dimensions"] = dimensions

                metric_data.append(metric_datum)

            # Send metrics in batches (CloudWatch limit is 20 per request)
            batch_size = 20
            for i in range(0, len(metric_data), batch_size):
                batch = metric_data[i : i + batch_size]

                self.cloudwatch.put_metric_data(Namespace=self.namespace, MetricData=batch)

            self.logger.debug(f"Sent {len(metrics)} metrics to CloudWatch")

        except Exception as e:
            self.logger.error(f"Failed to send metrics to CloudWatch: {e}")
            raise

    def _map_unit_to_cloudwatch(self, unit: MetricUnit) -> str:
        """Map our metric units to CloudWatch units"""
        mapping = {
            MetricUnit.COUNT: "Count",
            MetricUnit.PERCENT: "Percent",
            MetricUnit.SECONDS: "Seconds",
            MetricUnit.MILLISECONDS: "Milliseconds",
            MetricUnit.BYTES: "Bytes",
            MetricUnit.MEGABYTES: "Megabytes",
            MetricUnit.REQUESTS_PER_SECOND: "Count/Second",
            MetricUnit.ERRORS_PER_SECOND: "Count/Second",
        }
        return mapping.get(unit, "Count")

    async def create_alert(self, alert: Alert) -> str:
        """Create CloudWatch alarm"""
        if not self._connected:
            raise RuntimeError("Not connected to CloudWatch")

        try:
            alarm_name = f"{self.config.metric_prefix}-{alert.name}"

            # Map severity to CloudWatch actions
            actions = []
            if alert.notification_channels:
                # In real implementation, these would be SNS topic ARNs
                actions = alert.notification_channels

            # Create alarm
            self.cloudwatch.put_metric_alarm(
                AlarmName=alarm_name,
                AlarmDescription=alert.description,
                ActionsEnabled=alert.enabled,
                AlarmActions=actions,
                MetricName=alert.metric_name,
                Namespace=self.namespace,
                Statistic="Average",  # Could be configurable
                Dimensions=[{"Name": key, "Value": value} for key, value in alert.tags.items()],
                Period=300,  # 5 minutes
                EvaluationPeriods=2,
                Threshold=alert.threshold,
                ComparisonOperator=self._get_comparison_operator(alert.condition),
                TreatMissingData="breaching",
            )

            self.logger.info(f"Created CloudWatch alarm: {alarm_name}")
            return alarm_name

        except Exception as e:
            self.logger.error(f"Failed to create CloudWatch alarm: {e}")
            raise

    def _get_comparison_operator(self, condition: str) -> str:
        """Extract comparison operator from condition string"""
        if ">" in condition:
            return "GreaterThanThreshold"
        elif "<" in condition:
            return "LessThanThreshold"
        elif "==" in condition:
            return "LessThanThreshold"  # Default fallback
        else:
            return "GreaterThanThreshold"  # Default fallback

    async def update_alert(self, alert_id: str, alert: Alert) -> None:
        """Update CloudWatch alarm"""
        # CloudWatch alarms are updated by recreating with same name
        await self.delete_alert(alert_id)
        await self.create_alert(alert)

    async def delete_alert(self, alert_id: str) -> None:
        """Delete CloudWatch alarm"""
        if not self._connected:
            raise RuntimeError("Not connected to CloudWatch")

        try:
            self.cloudwatch.delete_alarms(AlarmNames=[alert_id])
            self.logger.info(f"Deleted CloudWatch alarm: {alert_id}")

        except Exception as e:
            self.logger.error(f"Failed to delete CloudWatch alarm: {e}")
            raise

    async def list_alerts(self) -> list[dict[str, Any]]:
        """List CloudWatch alarms"""
        if not self._connected:
            raise RuntimeError("Not connected to CloudWatch")

        try:
            response = self.cloudwatch.describe_alarms()

            alerts = []
            for alarm in response["MetricAlarms"]:
                alerts.append(
                    {
                        "id": alarm["AlarmName"],
                        "name": alarm["AlarmName"],
                        "description": alarm.get("AlarmDescription", ""),
                        "metric_name": alarm["MetricName"],
                        "threshold": alarm["Threshold"],
                        "state": alarm["StateValue"],
                        "enabled": alarm["ActionsEnabled"],
                    }
                )

            return alerts

        except Exception as e:
            self.logger.error(f"Failed to list CloudWatch alarms: {e}")
            raise

    async def create_dashboard(self, dashboard: Dashboard) -> str:
        """Create CloudWatch dashboard"""
        if not self._connected:
            raise RuntimeError("Not connected to CloudWatch")

        try:
            dashboard_name = f"{self.config.metric_prefix}-{dashboard.name.replace(' ', '-')}"

            # Convert widgets to CloudWatch format
            cw_widgets = []

            for widget in dashboard.widgets:
                if widget.get("type") == "metric_chart":
                    cw_widget = {
                        "type": "metric",
                        "properties": {
                            "metrics": [
                                [self.namespace, metric] for metric in widget.get("metrics", [])
                            ],
                            "period": 300,
                            "stat": "Average",
                            "region": self.region,
                            "title": widget.get("title", ""),
                        },
                    }
                    cw_widgets.append(cw_widget)

            # Create dashboard body
            dashboard_body = {"widgets": cw_widgets}

            self.cloudwatch.put_dashboard(
                DashboardName=dashboard_name, DashboardBody=json.dumps(dashboard_body)
            )

            self.logger.info(f"Created CloudWatch dashboard: {dashboard_name}")
            return dashboard_name

        except Exception as e:
            self.logger.error(f"Failed to create CloudWatch dashboard: {e}")
            raise

    async def update_dashboard(self, dashboard_id: str, dashboard: Dashboard) -> None:
        """Update CloudWatch dashboard"""
        # CloudWatch dashboards are updated by recreating with same name
        await self.create_dashboard(dashboard)

    async def delete_dashboard(self, dashboard_id: str) -> None:
        """Delete CloudWatch dashboard"""
        if not self._connected:
            raise RuntimeError("Not connected to CloudWatch")

        try:
            self.cloudwatch.delete_dashboards(DashboardNames=[dashboard_id])
            self.logger.info(f"Deleted CloudWatch dashboard: {dashboard_id}")

        except Exception as e:
            self.logger.error(f"Failed to delete CloudWatch dashboard: {e}")
            raise

    async def query_metrics(
        self,
        metric_name: str,
        start_time: datetime,
        end_time: datetime,
        tags: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Query CloudWatch metrics"""
        if not self._connected:
            raise RuntimeError("Not connected to CloudWatch")

        try:
            # Prepare dimensions from tags
            dimensions = []
            if tags:
                for key, value in tags.items():
                    dimensions.append({"Name": key, "Value": value})

            response = self.cloudwatch.get_metric_statistics(
                Namespace=self.namespace,
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=start_time,
                EndTime=end_time,
                Period=300,  # 5 minutes
                Statistics=["Average", "Sum", "Maximum", "Minimum"],
            )

            # Convert to our format
            data_points = []
            for point in response["Datapoints"]:
                data_points.append(
                    {
                        "timestamp": point["Timestamp"].isoformat(),
                        "average": point.get("Average"),
                        "sum": point.get("Sum"),
                        "maximum": point.get("Maximum"),
                        "minimum": point.get("Minimum"),
                    }
                )

            # Sort by timestamp
            data_points.sort(key=lambda x: x["timestamp"])

            return data_points

        except Exception as e:
            self.logger.error(f"Failed to query CloudWatch metrics: {e}")
            raise

    async def get_health(self) -> dict[str, Any]:
        """Get CloudWatch health status"""
        health = {
            "backend": "cloudwatch",
            "connected": self._connected,
            "region": self.region,
            "namespace": self.namespace,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if self._connected:
            try:
                # Test connectivity by listing metrics
                response = self.cloudwatch.list_metrics(Namespace=self.namespace, MaxRecords=1)
                health["metrics_available"] = len(response.get("Metrics", []))
                health["status"] = "healthy"

            except Exception as e:
                health["status"] = "unhealthy"
                health["error"] = str(e)
        else:
            health["status"] = "disconnected"

        return health


class CloudWatchCollector(AbstractMonitoringCollector):
    """CloudWatch-specific metric collector"""

    async def collect_system_metrics(self) -> list[Metric]:
        """Collect system metrics"""
        metrics = []

        try:
            import psutil

            # CPU usage
            cpu_percent = psutil.cpu_percent(interval=1)
            metrics.append(
                Metric(
                    name="system.cpu.usage",
                    value=cpu_percent,
                    metric_type=MetricType.GAUGE,
                    unit=MetricUnit.PERCENT,
                    tags={"metric_type": "system"},
                )
            )

            # Memory usage
            memory = psutil.virtual_memory()
            metrics.append(
                Metric(
                    name="system.memory.usage",
                    value=memory.percent,
                    metric_type=MetricType.GAUGE,
                    unit=MetricUnit.PERCENT,
                    tags={"metric_type": "system"},
                )
            )

            metrics.append(
                Metric(
                    name="system.memory.available",
                    value=memory.available / 1024 / 1024,  # MB
                    metric_type=MetricType.GAUGE,
                    unit=MetricUnit.MEGABYTES,
                    tags={"metric_type": "system"},
                )
            )

            # Disk usage
            disk = psutil.disk_usage("/")
            metrics.append(
                Metric(
                    name="system.disk.usage",
                    value=(disk.used / disk.total) * 100,
                    metric_type=MetricType.GAUGE,
                    unit=MetricUnit.PERCENT,
                    tags={"metric_type": "system"},
                )
            )

        except ImportError:
            # psutil not available
            pass
        except Exception as e:
            logging.error(f"Failed to collect system metrics: {e}")

        return metrics

    async def collect_application_metrics(self) -> list[Metric]:
        """Collect application metrics"""
        metrics = []

        try:
            import os

            import psutil

            process = psutil.Process(os.getpid())

            # Process CPU usage
            cpu_percent = process.cpu_percent()
            metrics.append(
                Metric(
                    name="application.cpu.usage",
                    value=cpu_percent,
                    metric_type=MetricType.GAUGE,
                    unit=MetricUnit.PERCENT,
                    tags={"metric_type": "application"},
                )
            )

            # Process memory usage
            memory_info = process.memory_info()
            metrics.append(
                Metric(
                    name="application.memory.rss",
                    value=memory_info.rss / 1024 / 1024,  # MB
                    metric_type=MetricType.GAUGE,
                    unit=MetricUnit.MEGABYTES,
                    tags={"metric_type": "application"},
                )
            )

            # Number of threads
            metrics.append(
                Metric(
                    name="application.threads.count",
                    value=process.num_threads(),
                    metric_type=MetricType.GAUGE,
                    unit=MetricUnit.COUNT,
                    tags={"metric_type": "application"},
                )
            )

            # File descriptors (Unix only)
            try:
                metrics.append(
                    Metric(
                        name="application.file_descriptors.count",
                        value=process.num_fds(),
                        metric_type=MetricType.GAUGE,
                        unit=MetricUnit.COUNT,
                        tags={"metric_type": "application"},
                    )
                )
            except AttributeError:
                # Windows doesn't have num_fds
                pass

        except Exception as e:
            logging.error(f"Failed to collect application metrics: {e}")

        return metrics

    async def collect_business_metrics(self) -> list[Metric]:
        """Collect business metrics"""
        # This would be implemented by specific services
        # For now, return empty list
        return []


# Enhanced CloudWatch client with LocalStack support
class LocalStackCloudWatchClient(CloudWatchClient):
    """CloudWatch client optimized for LocalStack"""

    def __init__(self, config: MonitoringConfig):
        super().__init__(config)

        # LocalStack-specific settings
        if not self.endpoint_url:
            self.endpoint_url = "http://localhost:4566"

        self.logger = logging.getLogger("localstack_cloudwatch")

    async def connect(self) -> None:
        """Connect to LocalStack CloudWatch"""
        # Override to set LocalStack-specific settings
        await super().connect()
        self.logger.info("Connected to LocalStack CloudWatch")


# Register CloudWatch clients
MonitoringFactory.register_client("cloudwatch", CloudWatchMonitoringClient)
MonitoringFactory.register_client("localstack", LocalStackCloudWatchClient)
