"""
Cliffracer - High-performance microservices framework built on NATS

A comprehensive, production-ready microservices framework that provides:
- Sub-millisecond message routing
- Type-safe service communication
- Built-in monitoring and observability
- Event-driven architecture patterns
"""

__version__ = "0.1.0"

# Core exports
from cliffracer.core.base_service import (
    BaseNATSService,
    NATSService,
    NATSServiceMeta,
    async_rpc,
    rpc,
)
from cliffracer.core.extended_service import (
    BroadcastMessage,
    HTTPNATSService,
    Message,
    RPCRequest,
    RPCResponse,
    ValidatedNATSService,
    broadcast,
    listener,
    validated_rpc,
)
from cliffracer.core.service_config import ServiceConfig

# Debug exports
from cliffracer.debug import BackdoorClient, BackdoorServer

# Logging exports
from cliffracer.logging import LoggedExtendedService, LoggingConfig, get_service_logger

# Runner exports
from cliffracer.runners.orchestrator import ServiceOrchestrator, ServiceRunner

# Auth exports (optional)
try:
    from cliffracer.auth.framework import AuthenticatedService, require_auth
    from cliffracer.auth.middleware import AuthMiddleware

    _auth_available = True
except ImportError:
    _auth_available = False

__all__ = [
    # Version
    "__version__",
    # Core classes
    "BaseNATSService",
    "NATSService",
    "NATSServiceMeta",
    "ServiceConfig",
    # Extended classes
    "ValidatedNATSService",
    "HTTPNATSService",
    "LoggedExtendedService",
    # Decorators
    "rpc",
    "async_rpc",
    "validated_rpc",
    "broadcast",
    "listener",
    # Message types
    "Message",
    "RPCRequest",
    "RPCResponse",
    "BroadcastMessage",
    # Runners
    "ServiceRunner",
    "ServiceOrchestrator",
    # Debug
    "BackdoorServer",
    "BackdoorClient",
    # Utils
    "LoggingConfig",
    "get_service_logger",
]

# Add auth exports if available
if _auth_available:
    __all__.extend(
        [
            "AuthenticatedService",
            "require_auth",
            "AuthMiddleware",
        ]
    )
