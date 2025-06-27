"""
Abstract runner interface for different service execution environments
Supports Docker containers, AWS Lambda, Google Cloud Functions, Azure Functions, etc.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class RunnerType(str, Enum):
    """Service runner types"""

    PROCESS = "process"  # Standard process runner
    DOCKER = "docker"  # Docker container runner
    LAMBDA = "lambda"  # AWS Lambda runner
    CLOUD_FUNCTION = "gcp"  # Google Cloud Functions
    AZURE_FUNCTION = "azure"  # Azure Functions
    KUBERNETES = "kubernetes"  # Kubernetes pod runner


class RuntimeEnvironment(str, Enum):
    """Runtime environment types"""

    DEVELOPMENT = "development"  # Local development
    STAGING = "staging"  # Staging environment
    PRODUCTION = "production"  # Production environment
    TESTING = "testing"  # Testing environment


@dataclass
class RunnerConfig:
    """Configuration for service runners"""

    runner_type: RunnerType
    environment: RuntimeEnvironment
    auto_restart: bool = True
    timeout_seconds: int = 900  # 15 minutes default
    memory_mb: int = 512
    max_concurrent: int = 100
    environment_variables: dict[str, str] = None
    tags: dict[str, str] = None

    def __post_init__(self):
        if self.environment_variables is None:
            self.environment_variables = {}
        if self.tags is None:
            self.tags = {}


@dataclass
class ServiceRequest:
    """Request to execute a service method"""

    service_name: str
    method_name: str
    payload: dict[str, Any]
    correlation_id: str
    reply_to: str | None = None
    headers: dict[str, str] = None
    timeout: int | None = None

    def __post_init__(self):
        if self.headers is None:
            self.headers = {}


@dataclass
class ServiceResponse:
    """Response from service method execution"""

    success: bool
    result: Any = None
    error: str | None = None
    execution_time_ms: float | None = None
    memory_used_mb: float | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class ServiceRunner(ABC):
    """Abstract base class for service runners"""

    def __init__(self, config: RunnerConfig):
        self.config = config
        self.logger = logging.getLogger(f"runner.{config.runner_type.value}")
        self._running = False
        self._service_instances: dict[str, Any] = {}

    @abstractmethod
    async def start(self) -> None:
        """Start the runner"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the runner"""
        pass

    @abstractmethod
    async def execute_service_method(self, request: ServiceRequest) -> ServiceResponse:
        """Execute a service method"""
        pass

    @abstractmethod
    async def register_service(self, service_class: type, service_config: Any) -> str:
        """Register a service class for execution"""
        pass

    @abstractmethod
    async def unregister_service(self, service_id: str) -> None:
        """Unregister a service"""
        pass

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        """Get runner statistics"""
        pass

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """Perform health check"""
        pass

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def runner_type(self) -> RunnerType:
        return self.config.runner_type


class AbstractServiceOrchestrator(ABC):
    """Abstract orchestrator for managing multiple services"""

    def __init__(self, runner: ServiceRunner):
        self.runner = runner
        self.services: dict[str, dict] = {}
        self.logger = logging.getLogger("orchestrator")

    @abstractmethod
    async def deploy_service(
        self, service_class: type, service_config: Any, deployment_config: dict[str, Any] = None
    ) -> str:
        """Deploy a service"""
        pass

    @abstractmethod
    async def undeploy_service(self, service_id: str) -> None:
        """Undeploy a service"""
        pass

    @abstractmethod
    async def scale_service(self, service_id: str, target_instances: int) -> None:
        """Scale a service to target instances"""
        pass

    @abstractmethod
    async def get_service_logs(self, service_id: str, lines: int = 100) -> list[str]:
        """Get service logs"""
        pass

    async def list_services(self) -> dict[str, dict]:
        """List all deployed services"""
        return self.services.copy()


class RunnerFactory:
    """Factory for creating service runners"""

    _runners: dict[RunnerType, type[AbstractServiceRunner]] = {}

    @classmethod
    def register_runner(cls, runner_type: RunnerType, runner_class: type[AbstractServiceRunner]):
        """Register a runner implementation"""
        cls._runners[runner_type] = runner_class

    @classmethod
    def create_runner(cls, config: RunnerConfig) -> ServiceRunner:
        """Create a runner instance"""
        if config.runner_type not in cls._runners:
            raise ValueError(f"Unknown runner type: {config.runner_type}")

        return cls._runners[config.runner_type](config)

    @classmethod
    def list_runners(cls) -> list[RunnerType]:
        """List available runner types"""
        return list(cls._runners.keys())


# Utility functions for runner management


def get_execution_context() -> dict[str, Any]:
    """Get current execution context information"""
    import os
    import platform

    import psutil

    return {
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "hostname": platform.node(),
        "process_id": os.getpid(),
        "memory_usage_mb": psutil.Process().memory_info().rss / 1024 / 1024,
        "cpu_percent": psutil.cpu_percent(),
        "timestamp": datetime.utcnow().isoformat(),
    }


def create_correlation_id() -> str:
    """Create a unique correlation ID"""
    import uuid

    return str(uuid.uuid4())


class ServiceMetrics:
    """Metrics collection for service execution"""

    def __init__(self):
        self.requests_total = 0
        self.requests_success = 0
        self.requests_error = 0
        self.total_execution_time_ms = 0.0
        self.memory_usage_samples = []

    def record_request(self, response: ServiceResponse):
        """Record a service request"""
        self.requests_total += 1

        if response.success:
            self.requests_success += 1
        else:
            self.requests_error += 1

        if response.execution_time_ms:
            self.total_execution_time_ms += response.execution_time_ms

        if response.memory_used_mb:
            self.memory_usage_samples.append(response.memory_used_mb)

    def get_stats(self) -> dict[str, Any]:
        """Get metrics statistics"""
        avg_execution_time = 0.0
        if self.requests_total > 0:
            avg_execution_time = self.total_execution_time_ms / self.requests_total

        avg_memory_usage = 0.0
        if self.memory_usage_samples:
            avg_memory_usage = sum(self.memory_usage_samples) / len(self.memory_usage_samples)

        success_rate = 0.0
        if self.requests_total > 0:
            success_rate = self.requests_success / self.requests_total

        return {
            "requests_total": self.requests_total,
            "requests_success": self.requests_success,
            "requests_error": self.requests_error,
            "success_rate": success_rate,
            "avg_execution_time_ms": avg_execution_time,
            "avg_memory_usage_mb": avg_memory_usage,
            "total_execution_time_ms": self.total_execution_time_ms,
        }


# Decorators for runner-aware services


def runner_aware(runner_types: list[RunnerType] = None):
    """Decorator to mark services as compatible with specific runners"""

    def decorator(cls):
        cls._compatible_runners = runner_types or list(RunnerType)
        return cls

    return decorator


def execution_timeout(seconds: int):
    """Decorator to set execution timeout for service methods"""

    def decorator(func):
        func._execution_timeout = seconds
        return func

    return decorator


def memory_limit(mb: int):
    """Decorator to set memory limit for service methods"""

    def decorator(func):
        func._memory_limit_mb = mb
        return func

    return decorator


def cold_start_optimization(enabled: bool = True):
    """Decorator to enable cold start optimization"""

    def decorator(cls):
        cls._cold_start_optimization = enabled
        return cls

    return decorator


# Context manager for service execution


class ServiceExecutionContext:
    """Context manager for tracking service execution"""

    def __init__(self, request: ServiceRequest):
        self.request = request
        self.start_time = None
        self.end_time = None
        self.memory_start = None
        self.memory_end = None

    async def __aenter__(self):
        self.start_time = datetime.utcnow()
        try:
            import psutil

            self.memory_start = psutil.Process().memory_info().rss / 1024 / 1024
        except ImportError:
            pass
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.end_time = datetime.utcnow()
        try:
            import psutil

            self.memory_end = psutil.Process().memory_info().rss / 1024 / 1024
        except ImportError:
            pass

    def get_execution_time_ms(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds() * 1000
        return 0.0

    def get_memory_used_mb(self) -> float | None:
        if self.memory_start is not None and self.memory_end is not None:
            return self.memory_end - self.memory_start
        return None
