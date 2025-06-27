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
    rpc,
    async_rpc,
)
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.extended_service import (
    ValidatedNATSService,
    HTTPNATSService,
    validated_rpc,
    broadcast,
    listener,
    Message,
    RPCRequest,
    RPCResponse,
    BroadcastMessage,
)

# Logging exports
from cliffracer.logging import LoggingConfig, get_service_logger
from cliffracer.logging import LoggedExtendedService

# Runner exports
from cliffracer.runners.orchestrator import ServiceRunner, ServiceOrchestrator

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
    
    # Utils
    "LoggingConfig",
    "get_service_logger",
]

# Add auth exports if available
if _auth_available:
    __all__.extend([
        "AuthenticatedService",
        "require_auth", 
        "AuthMiddleware",
    ])