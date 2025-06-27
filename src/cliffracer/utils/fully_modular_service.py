"""
Fully modular service implementation supporting any combination of:
- Messaging: NATS, AWS SNS/SQS, Google Pub/Sub, Azure Service Bus
- Runners: Process, Docker, Lambda, Cloud Functions, Azure Functions
- Monitoring: Zabbix, CloudWatch, Prometheus, Datadog
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Messaging imports
from messaging import MessagingConfig, MessageClientFactory
from messaging.abstract_messaging import MessageBroker, MessageClient

# Monitoring imports
from monitoring.abstract_monitoring import (
    MonitoringClient,
    MetricType,
    MetricUnit,
    MonitoringConfig,
    MonitoringFactory,
    count_requests,
    monitor_performance,
)

# Runner imports
from runners.abstract_runner import (
    ServiceRunner,
    RunnerConfig,
    RunnerFactory,
    RunnerType,
    RuntimeEnvironment,
)


@dataclass
class FullyModularConfig:
    """Configuration for fully modular services"""

    service_name: str

    # Messaging configuration
    messaging: MessagingConfig

    # Runner configuration
    runner: RunnerConfig

    # Monitoring configuration
    monitoring: MonitoringConfig

    # Service-specific settings
    auto_restart: bool = True
    health_check_interval: int = 30
    metrics_collection_interval: int = 60

    # Environment settings
    environment: RuntimeEnvironment = RuntimeEnvironment.DEVELOPMENT
    log_level: str = "INFO"


class PluggableNATSService:
    """
    Base service class that can use any combination of messaging, runner, and monitoring backends
    """

    def __init__(self, config: FullyModularConfig):
        self.config = config
        self.logger = self._setup_logging()

        # Initialize components
        self.messaging_client: MessageClient | None = None
        self.messaging_broker: MessageBroker | None = None
        self.runner: ServiceRunner | None = None
        self.monitoring_client: MonitoringClient | None = None

        # Service state
        self._running = False
        self._handlers: dict[str, Any] = {}
        self._health_check_task: asyncio.Task | None = None
        self._metrics_task: asyncio.Task | None = None

        # Discover handlers
        self._discover_handlers()

    def _setup_logging(self) -> logging.Logger:
        """Setup service logging"""
        logger = logging.getLogger(f"service.{self.config.service_name}")
        logger.setLevel(getattr(logging, self.config.log_level))

        # Create handler if not exists
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                f"%(asctime)s - {self.config.service_name} - %(levelname)s - %(message)s"
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        return logger

    def _discover_handlers(self):
        """Discover RPC and event handlers from decorators"""
        for attr_name in dir(self):
            attr = getattr(self, attr_name)

            if hasattr(attr, "_is_rpc"):
                self._handlers[f"rpc_{attr._rpc_name}"] = attr
                self.logger.debug(f"Registered RPC handler: {attr._rpc_name}")

            elif hasattr(attr, "_is_event_handler"):
                self._handlers[f"event_{attr._event_pattern}"] = attr
                self.logger.debug(f"Registered event handler: {attr._event_pattern}")

    async def initialize(self) -> None:
        """Initialize all service components"""
        try:
            # Initialize messaging
            self.messaging_client = MessagingFactory.create_client(
                self.config.messaging.backend, **self.config.messaging.connection_params
            )
            await self.messaging_client.connect()

            # Create message broker
            from messaging.aws_messaging import AWSMessageBroker
            from messaging.nats_messaging import NATSMessageBroker

            if self.config.messaging.backend == "nats":
                self.messaging_broker = NATSMessageBroker(self.messaging_client)
            elif self.config.messaging.backend == "aws":
                self.messaging_broker = AWSMessageBroker(self.messaging_client)
            else:
                # Generic broker
                from messaging.abstract_messaging import MessageBroker

                self.messaging_broker = MessageBroker(self.messaging_client)

            # Initialize runner
            self.runner = RunnerFactory.create_runner(self.config.runner)
            await self.runner.start()

            # Initialize monitoring
            self.monitoring_client = MonitoringFactory.create_client(self.config.monitoring)
            await self.monitoring_client.connect()

            self.logger.info(
                f"Initialized service with {self.config.messaging.backend} messaging, "
                f"{self.config.runner.runner_type.value} runner, "
                f"{self.config.monitoring.backend} monitoring"
            )

        except Exception as e:
            self.logger.error(f"Failed to initialize service: {e}")
            raise

    async def start(self) -> None:
        """Start the service"""
        if self._running:
            return

        await self.initialize()

        # Register service with runner if not Lambda (Lambda handles this differently)
        if self.config.runner.runner_type != RunnerType.LAMBDA:
            await self.runner.register_service(self.__class__, self.config)

        # Start background tasks
        await self._start_background_tasks()

        # Service-specific startup
        await self.on_startup()

        self._running = True
        self.logger.info(f"Service '{self.config.service_name}' started")

    async def stop(self) -> None:
        """Stop the service"""
        if not self._running:
            return

        # Stop background tasks
        await self._stop_background_tasks()

        # Service-specific shutdown
        await self.on_shutdown()

        # Cleanup components
        if self.runner:
            await self.runner.stop()

        if self.monitoring_client:
            await self.monitoring_client.disconnect()

        if self.messaging_client:
            await self.messaging_client.disconnect()

        self._running = False
        self.logger.info(f"Service '{self.config.service_name}' stopped")

    async def _start_background_tasks(self) -> None:
        """Start background monitoring and health check tasks"""
        # Health check task
        if self.config.health_check_interval > 0:
            self._health_check_task = asyncio.create_task(self._health_check_loop())

        # Metrics collection task
        if self.config.metrics_collection_interval > 0:
            self._metrics_task = asyncio.create_task(self._metrics_loop())

    async def _stop_background_tasks(self) -> None:
        """Stop background tasks"""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        if self._metrics_task:
            self._metrics_task.cancel()
            try:
                await self._metrics_task
            except asyncio.CancelledError:
                pass

    async def _health_check_loop(self) -> None:
        """Background health check loop"""
        while self._running:
            try:
                health = await self.health_check()

                # Record health metric
                if self.monitoring_client:
                    await self.monitoring_client.record_metric(
                        name="service.health",
                        value=1 if health.get("status") == "healthy" else 0,
                        metric_type=MetricType.GAUGE,
                        tags={"service": self.config.service_name},
                    )

                await asyncio.sleep(self.config.health_check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health check failed: {e}")
                await asyncio.sleep(self.config.health_check_interval)

    async def _metrics_loop(self) -> None:
        """Background metrics collection loop"""
        while self._running:
            try:
                # Collect and send metrics
                await self._collect_service_metrics()
                await asyncio.sleep(self.config.metrics_collection_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Metrics collection failed: {e}")
                await asyncio.sleep(self.config.metrics_collection_interval)

    async def _collect_service_metrics(self) -> None:
        """Collect service-specific metrics"""
        if not self.monitoring_client:
            return

        try:
            # Service uptime
            await self.monitoring_client.record_metric(
                name="service.uptime",
                value=1,
                metric_type=MetricType.GAUGE,
                tags={"service": self.config.service_name},
            )

            # Handler count
            await self.monitoring_client.record_metric(
                name="service.handlers.count",
                value=len(self._handlers),
                metric_type=MetricType.GAUGE,
                tags={"service": self.config.service_name},
            )

            # Component health
            components_healthy = 0
            total_components = 0

            if self.messaging_client:
                total_components += 1
                if self.messaging_client.is_connected:
                    components_healthy += 1

            if self.runner:
                total_components += 1
                if self.runner.is_running:
                    components_healthy += 1

            if self.monitoring_client:
                total_components += 1
                if self.monitoring_client.is_connected:
                    components_healthy += 1

            if total_components > 0:
                health_percentage = (components_healthy / total_components) * 100
                await self.monitoring_client.record_metric(
                    name="service.components.health",
                    value=health_percentage,
                    metric_type=MetricType.GAUGE,
                    unit=MetricUnit.PERCENT,
                    tags={"service": self.config.service_name},
                )

        except Exception as e:
            self.logger.error(f"Failed to collect service metrics: {e}")

    # Service lifecycle hooks

    async def on_startup(self) -> None:
        """Called when service starts - override in subclasses"""
        pass

    async def on_shutdown(self) -> None:
        """Called when service stops - override in subclasses"""
        pass

    # Messaging operations

    @monitor_performance("rpc_call_time")
    @count_requests("rpc_calls")
    async def call_rpc(self, service: str, method: str, timeout: float = 30.0, **kwargs) -> Any:
        """Call remote procedure"""
        return await self.messaging_broker.call_rpc(service, method, timeout, **kwargs)

    @count_requests("async_calls")
    async def call_async(self, service: str, method: str, **kwargs) -> None:
        """Call remote procedure asynchronously"""
        await self.messaging_broker.call_async(service, method, **kwargs)

    @count_requests("events_published")
    async def publish_event(self, subject: str, **kwargs) -> None:
        """Publish an event"""
        await self.messaging_broker.publish_event(subject, **kwargs)

    # Health and statistics

    async def health_check(self) -> dict[str, Any]:
        """Perform comprehensive health check"""
        health = {
            "service": self.config.service_name,
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "components": {},
        }

        # Check messaging
        if self.messaging_client:
            health["components"]["messaging"] = {
                "backend": self.config.messaging.backend,
                "connected": self.messaging_client.is_connected,
                "status": "healthy" if self.messaging_client.is_connected else "unhealthy",
            }
            if not self.messaging_client.is_connected:
                health["status"] = "unhealthy"

        # Check runner
        if self.runner:
            health["components"]["runner"] = {
                "type": self.config.runner.runner_type.value,
                "running": self.runner.is_running,
                "status": "healthy" if self.runner.is_running else "unhealthy",
            }
            if not self.runner.is_running:
                health["status"] = "degraded"

        # Check monitoring
        if self.monitoring_client:
            health["components"]["monitoring"] = {
                "backend": self.config.monitoring.backend,
                "connected": self.monitoring_client.is_connected,
                "status": "healthy" if self.monitoring_client.is_connected else "degraded",
            }

        return health

    async def get_stats(self) -> dict[str, Any]:
        """Get comprehensive service statistics"""
        stats = {
            "service": self.config.service_name,
            "running": self._running,
            "handlers": len(self._handlers),
            "config": {
                "messaging_backend": self.config.messaging.backend,
                "runner_type": self.config.runner.runner_type.value,
                "monitoring_backend": self.config.monitoring.backend,
                "environment": self.config.environment.value,
            },
        }

        # Add component stats
        if self.messaging_client:
            try:
                stats["messaging"] = await self.messaging_client.get_stats()
            except Exception as e:
                stats["messaging"] = {"error": str(e)}

        if self.runner:
            try:
                stats["runner"] = await self.runner.get_stats()
            except Exception as e:
                stats["runner"] = {"error": str(e)}

        if self.monitoring_client:
            try:
                stats["monitoring"] = await self.monitoring_client.get_health()
            except Exception as e:
                stats["monitoring"] = {"error": str(e)}

        return stats


# Decorators for modular services
def rpc(func):
    """Mark method as RPC handler"""
    func._is_rpc = True
    func._rpc_name = func.__name__
    return func


def event_handler(pattern: str):
    """Mark method as event handler"""

    def decorator(func):
        func._is_event_handler = True
        func._event_pattern = pattern
        return func

    return decorator


# Configuration factory for different deployment scenarios
class ConfigFactory:
    """Factory for creating configurations for different scenarios"""

    @staticmethod
    def local_development(service_name: str) -> FullyModularConfig:
        """Configuration for local development"""
        return FullyModularConfig(
            service_name=service_name,
            messaging=MessagingConfig.nats("nats://localhost:4222"),
            runner=RunnerConfig(
                runner_type=RunnerType.PROCESS, environment=RuntimeEnvironment.DEVELOPMENT
            ),
            monitoring=MonitoringConfig(
                backend="zabbix", connection_params={"host": "localhost", "port": 10051}
            ),
            environment=RuntimeEnvironment.DEVELOPMENT,
        )

    @staticmethod
    def aws_lambda_localstack(service_name: str) -> FullyModularConfig:
        """Configuration for AWS Lambda with LocalStack"""
        return FullyModularConfig(
            service_name=service_name,
            messaging=MessagingConfig.aws_sns_sqs(
                region="us-east-1", access_key_id="test", secret_access_key="test"
            ),
            runner=RunnerConfig(
                runner_type=RunnerType.LAMBDA,
                environment=RuntimeEnvironment.DEVELOPMENT,
                environment_variables={
                    "AWS_ENDPOINT_URL": "http://localhost:4566",
                    "LAMBDA_PREFIX": "cliffracer-dev",
                },
            ),
            monitoring=MonitoringConfig(
                backend="localstack",
                connection_params={"region": "us-east-1", "endpoint_url": "http://localhost:4566"},
            ),
            environment=RuntimeEnvironment.DEVELOPMENT,
        )

    @staticmethod
    def aws_production(service_name: str, region: str = "us-east-1") -> FullyModularConfig:
        """Configuration for AWS production"""
        return FullyModularConfig(
            service_name=service_name,
            messaging=MessagingConfig.aws_sns_sqs(region=region),
            runner=RunnerConfig(
                runner_type=RunnerType.LAMBDA,
                environment=RuntimeEnvironment.PRODUCTION,
                timeout_seconds=300,
                memory_mb=1024,
            ),
            monitoring=MonitoringConfig(backend="cloudwatch", connection_params={"region": region}),
            environment=RuntimeEnvironment.PRODUCTION,
        )

    @staticmethod
    def hybrid_nats_lambda(service_name: str) -> FullyModularConfig:
        """Configuration for NATS messaging with Lambda runner"""
        return FullyModularConfig(
            service_name=service_name,
            messaging=MessagingConfig.nats("nats://nats-cluster:4222"),
            runner=RunnerConfig(
                runner_type=RunnerType.LAMBDA, environment=RuntimeEnvironment.PRODUCTION
            ),
            monitoring=MonitoringConfig(
                backend="cloudwatch", connection_params={"region": "us-east-1"}
            ),
            environment=RuntimeEnvironment.PRODUCTION,
        )

    @staticmethod
    def from_environment(service_name: str) -> FullyModularConfig:
        """Create configuration from environment variables"""
        # Messaging configuration
        messaging_backend = os.getenv("MESSAGING_BACKEND", "nats")
        if messaging_backend == "nats":
            messaging = MessagingConfig.nats(url=os.getenv("NATS_URL", "nats://localhost:4222"))
        elif messaging_backend == "aws":
            messaging = MessagingConfig.aws_sns_sqs(region=os.getenv("AWS_REGION", "us-east-1"))
        else:
            raise ValueError(f"Unknown messaging backend: {messaging_backend}")

        # Runner configuration
        runner_type = RunnerType(os.getenv("RUNNER_TYPE", "process"))
        runner = RunnerConfig(
            runner_type=runner_type,
            environment=RuntimeEnvironment(os.getenv("ENVIRONMENT", "development")),
            environment_variables=dict(os.environ),
        )

        # Monitoring configuration
        monitoring_backend = os.getenv("MONITORING_BACKEND", "zabbix")
        if monitoring_backend == "zabbix":
            monitoring = MonitoringConfig(
                backend="zabbix",
                connection_params={
                    "host": os.getenv("ZABBIX_HOST", "localhost"),
                    "port": int(os.getenv("ZABBIX_PORT", "10051")),
                },
            )
        elif monitoring_backend == "cloudwatch":
            monitoring = MonitoringConfig(
                backend="cloudwatch",
                connection_params={
                    "region": os.getenv("AWS_REGION", "us-east-1"),
                    "endpoint_url": os.getenv("AWS_ENDPOINT_URL"),
                },
            )
        else:
            monitoring = MonitoringConfig(backend=monitoring_backend, connection_params={})

        return FullyModularConfig(
            service_name=service_name,
            messaging=messaging,
            runner=runner,
            monitoring=monitoring,
            environment=RuntimeEnvironment(os.getenv("ENVIRONMENT", "development")),
        )
