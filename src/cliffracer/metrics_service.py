"""
Metrics collection and export service for Zabbix integration
"""

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp

from nats_service_extended import ValidatedNATSService, ServiceConfig, rpc


@dataclass
class ServiceMetrics:
    """Service metrics data structure"""

    service_name: str
    timestamp: datetime
    status: str
    nats_connected: bool
    rpc_methods_count: int
    event_handlers_count: int
    response_time_ms: float
    error_rate: float = 0.0
    request_count: int = 0


@dataclass
class NATSMetrics:
    """NATS server metrics data structure"""

    timestamp: datetime
    connections: int
    messages_in: int
    messages_out: int
    bytes_in: int
    bytes_out: int
    uptime: str
    memory_usage: int = 0
    cpu_usage: float = 0.0


class SystemMetricsCollector:
    """Collects metrics from various sources"""

    def __init__(self):
        self.session: aiohttp.ClientSession = None
        self.service_metrics: dict[str, ServiceMetrics] = {}
        self.nats_metrics: NATSMetrics = None

    async def start(self):
        """Start the metrics collector"""
        self.session = aiohttp.ClientSession()

    async def stop(self):
        """Stop the metrics collector"""
        if self.session:
            await self.session.close()

    async def collect_service_metrics(self, service_name: str, port: int) -> ServiceMetrics:
        """Collect metrics from a service"""
        base_url = f"http://{service_name}:{port}"

        try:
            # Measure response time
            start_time = time.time()

            # Get health status
            async with self.session.get(f"{base_url}/health", timeout=5) as response:
                health_data = await response.json()
                response_time = (time.time() - start_time) * 1000

            # Get service info
            async with self.session.get(f"{base_url}/info", timeout=5) as response:
                info_data = await response.json()

            return ServiceMetrics(
                service_name=service_name,
                timestamp=datetime.now(UTC),
                status=health_data.get("status", "unknown"),
                nats_connected=health_data.get("nats_connected", False),
                rpc_methods_count=len(info_data.get("rpc_methods", [])),
                event_handlers_count=len(info_data.get("event_handlers", [])),
                response_time_ms=response_time,
            )

        except Exception:
            return ServiceMetrics(
                service_name=service_name,
                timestamp=datetime.now(UTC),
                status="unreachable",
                nats_connected=False,
                rpc_methods_count=0,
                event_handlers_count=0,
                response_time_ms=0.0,
                error_rate=1.0,
            )

    async def collect_nats_metrics(self) -> NATSMetrics:
        """Collect NATS server metrics"""
        try:
            # Get connection info
            async with self.session.get("http://nats:8222/connz", timeout=5) as response:
                conn_data = await response.json()

            # Get variable stats
            async with self.session.get("http://nats:8222/varz", timeout=5) as response:
                var_data = await response.json()

            return NATSMetrics(
                timestamp=datetime.now(UTC),
                connections=conn_data.get("num_connections", 0),
                messages_in=var_data.get("in_msgs", 0),
                messages_out=var_data.get("out_msgs", 0),
                bytes_in=var_data.get("in_bytes", 0),
                bytes_out=var_data.get("out_bytes", 0),
                uptime=var_data.get("uptime", "unknown"),
                memory_usage=var_data.get("mem", 0),
                cpu_usage=var_data.get("cpu", 0.0),
            )

        except Exception:
            return NATSMetrics(
                timestamp=datetime.now(UTC),
                connections=0,
                messages_in=0,
                messages_out=0,
                bytes_in=0,
                bytes_out=0,
                uptime="unknown",
            )


class ZabbixExporter:
    """Sends metrics to Zabbix server"""

    def __init__(self, zabbix_server: str, zabbix_port: int = 10051):
        self.zabbix_server = zabbix_server
        self.zabbix_port = zabbix_port

    async def send_metrics(self, hostname: str, metrics: list[dict[str, Any]]):
        """Send metrics to Zabbix using zabbix_sender protocol"""
        # For simplicity, we'll write to a file that Zabbix agent can read
        # In production, you'd use the actual zabbix_sender protocol

        metrics_file = f"/tmp/zabbix_metrics_{hostname}.json"

        try:
            with open(metrics_file, "w") as f:
                json.dump(
                    {
                        "hostname": hostname,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "metrics": metrics,
                    },
                    f,
                    indent=2,
                )

            print(f"Metrics written to {metrics_file}")

        except Exception as e:
            print(f"Error writing metrics: {e}")


class ZabbixMetricsService(ValidatedNATSService):
    """Service that collects and exports metrics to Zabbix"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.collector = SystemMetricsCollector()
        self.zabbix_sender = ZabbixExporter(os.getenv("ZABBIX_SERVER", "zabbix-server"))
        self.collection_interval = 30  # seconds
        self.services_to_monitor = [
            ("user_service", 8001),
            ("notification_service", 8002),
            ("analytics_service", 8000),  # Assuming analytics doesn't have HTTP
        ]
        self._collection_task = None

    async def start(self):
        """Start the metrics service"""
        await super().start()
        await self.collector.start()

        # Start metrics collection loop
        self._collection_task = asyncio.create_task(self._collection_loop())

    async def stop(self):
        """Stop the metrics service"""
        if self._collection_task:
            self._collection_task.cancel()

        await self.collector.stop()
        await super().stop()

    async def _collection_loop(self):
        """Main metrics collection loop"""
        while True:
            try:
                await self._collect_and_export_metrics()
                await asyncio.sleep(self.collection_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in metrics collection: {e}")
                await asyncio.sleep(self.collection_interval)

    async def _collect_and_export_metrics(self):
        """Collect metrics from all sources and export to Zabbix"""
        metrics_to_send = []

        # Collect NATS metrics
        nats_metrics = await self.collector.collect_nats_metrics()
        metrics_to_send.extend(
            [
                {"key": "nats.server.connections", "value": nats_metrics.connections},
                {"key": "nats.server.messages.in", "value": nats_metrics.messages_in},
                {"key": "nats.server.messages.out", "value": nats_metrics.messages_out},
                {"key": "nats.server.bytes.in", "value": nats_metrics.bytes_in},
                {"key": "nats.server.bytes.out", "value": nats_metrics.bytes_out},
                {"key": "nats.server.uptime", "value": nats_metrics.uptime},
            ]
        )

        # Collect service metrics
        for service_name, port in self.services_to_monitor:
            service_metrics = await self.collector.collect_service_metrics(service_name, port)
            metrics_to_send.extend(
                [
                    {"key": f"service.{service_name}.status", "value": service_metrics.status},
                    {
                        "key": f"service.{service_name}.nats_connected",
                        "value": int(service_metrics.nats_connected),
                    },
                    {
                        "key": f"service.{service_name}.response_time",
                        "value": service_metrics.response_time_ms,
                    },
                    {
                        "key": f"service.{service_name}.rpc_methods",
                        "value": service_metrics.rpc_methods_count,
                    },
                    {
                        "key": f"service.{service_name}.event_handlers",
                        "value": service_metrics.event_handlers_count,
                    },
                ]
            )

        # Send to Zabbix
        await self.zabbix_sender.send_metrics("NATS-Services-Host", metrics_to_send)

        # Store locally for HTTP access
        await self._store_metrics_locally(nats_metrics, metrics_to_send)

    async def _store_metrics_locally(self, nats_metrics: NATSMetrics, all_metrics: list[dict]):
        """Store metrics locally for HTTP endpoint access"""
        metrics_data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "nats": asdict(nats_metrics),
            "services": {},
            "all_metrics": all_metrics,
        }

        # Group service metrics
        for metric in all_metrics:
            if metric["key"].startswith("service."):
                parts = metric["key"].split(".")
                if len(parts) >= 3:
                    service_name = parts[1]
                    metric_name = ".".join(parts[2:])

                    if service_name not in metrics_data["services"]:
                        metrics_data["services"][service_name] = {}
                    metrics_data["services"][service_name][metric_name] = metric["value"]

        # Write to metrics directory
        os.makedirs("/app/metrics", exist_ok=True)
        with open("/app/metrics/current.json", "w") as f:
            json.dump(metrics_data, f, indent=2)

    @rpc
    async def get_current_metrics(self):
        """Get current metrics via RPC"""
        try:
            with open("/app/metrics/current.json") as f:
                return json.load(f)
        except FileNotFoundError:
            return {"error": "No metrics available yet"}

    @rpc
    async def get_service_metrics(self, service_name: str):
        """Get metrics for a specific service"""
        try:
            with open("/app/metrics/current.json") as f:
                data = json.load(f)
                return data.get("services", {}).get(service_name, {})
        except FileNotFoundError:
            return {"error": f"No metrics available for {service_name}"}

    @rpc
    async def get_nats_metrics(self):
        """Get NATS server metrics"""
        try:
            with open("/app/metrics/current.json") as f:
                data = json.load(f)
                return data.get("nats", {})
        except FileNotFoundError:
            return {"error": "No NATS metrics available"}


if __name__ == "__main__":
    from nats_runner import ServiceRunner, configure_logging

    configure_logging()

    config = ServiceConfig(
        name="metrics_exporter",
        nats_url=os.getenv("NATS_URL", "nats://localhost:4222"),
        auto_restart=True,
    )

    runner = ServiceRunner(ZabbixMetricsService, config)
    runner.run_forever()
