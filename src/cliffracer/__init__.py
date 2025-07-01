"""
Cliffracer - High-performance microservices framework built on NATS

A comprehensive, production-ready microservices framework that provides:
- Sub-millisecond message routing
- Type-safe service communication
- Built-in monitoring and observability
- Event-driven architecture patterns
"""

__version__ = "1.0.0"

# Core exports - Consolidated Service Architecture
from cliffracer.core.consolidated_service import (
    BaseNATSService,  # Legacy alias
    BroadcastNATSService,
    CliffracerService,
    ExtendedNATSService,  # Legacy alias
    FullFeaturedService,
    HTTPNATSService,
    HighPerformanceService,
    NATSService,
    ValidatedNATSService,
    WebSocketNATSService,
)

# Decorator exports - All decorators in one place
from cliffracer.core.decorators import (
    async_rpc,
    broadcast,
    cache_result,
    compose_decorators,
    get,
    http_endpoint,
    listener,
    monitor_performance,
    post,
    retry,
    robust_rpc,
    rpc,
    scheduled_task,
    timer,
    validated_rpc,
    websocket_handler,
)

# Message types from consolidated service
from cliffracer.core.extended_service import (
    BroadcastMessage,
    Message,
    RPCRequest,
    RPCResponse,
)

# Exception hierarchy
from cliffracer.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    CliffracerError,
    ConfigurationError,
    ConnectionError,
    DatabaseError,
    ErrorHandler,
    HTTPError,
    HandlerError,
    PerformanceError,
    RPCError,
    ServiceError,
    TimerError,
    ValidationError,
    WebSocketError,
)

# Timer class
from cliffracer.core.timer import Timer

# Configuration
from cliffracer.core.service_config import ServiceConfig

# Correlation ID support
from cliffracer.core.correlation import (
    CorrelationContext,
    get_correlation_id,
    set_correlation_id,
    create_correlation_id,
    with_correlation_id,
)
from cliffracer.middleware.correlation import (
    CorrelationMiddleware,
    WebSocketCorrelationMiddleware,
    correlation_id_dependency,
)
from cliffracer.logging.correlation_logging import (
    setup_correlation_logging,
    get_correlation_logger,
    CorrelationLoggerMixin,
)

# Database exports
from cliffracer.database import DatabaseConnection, DatabaseModel, Repository, get_db_connection
from cliffracer.database.secure_repository import SecureRepository

# Debug exports
from cliffracer.debug import BackdoorClient, BackdoorServer

# Logging exports
from cliffracer.logging import (
    HTTPLoggingMixin,
    LoggingConfig,
    LoggingMixin,
    WebSocketLoggingMixin,
    get_service_logger,
)
from cliffracer.performance import BatchProcessor, OptimizedNATSConnection, PerformanceMetrics

# Runner exports
from cliffracer.runners.orchestrator import ServiceOrchestrator, ServiceRunner

# Auth exports - New simple auth system
from cliffracer.auth.simple_auth import (
    AuthConfig,
    AuthUser,
    AuthContext,
    SimpleAuthService,
    AuthenticationError,
    AuthorizationError,
    requires_auth,
    requires_roles,
    requires_permissions,
    get_current_user,
    get_current_context,
    set_auth_service,
    AuthMiddleware,
)

__all__ = [
    # Version
    "__version__",
    
    # Core Service Classes - Consolidated Architecture
    "CliffracerService",
    "NATSService", 
    "ValidatedNATSService",
    "HTTPNATSService",
    "WebSocketNATSService",
    "BroadcastNATSService",
    "HighPerformanceService",
    "FullFeaturedService",
    # Legacy aliases
    "BaseNATSService",
    "ExtendedNATSService",
    
    # Configuration
    "ServiceConfig",
    "Timer",
    
    # Decorators - All in one place
    "rpc",
    "async_rpc", 
    "validated_rpc",
    "broadcast",
    "listener",
    "websocket_handler",
    "timer",
    "get",
    "post",
    "http_endpoint",
    "monitor_performance",
    "retry",
    "cache_result",
    "compose_decorators",
    "robust_rpc",
    "scheduled_task",
    
    # Message Types
    "Message",
    "RPCRequest", 
    "RPCResponse",
    "BroadcastMessage",
    
    # Exception Hierarchy
    "CliffracerError",
    "ServiceError",
    "ConnectionError",
    "ConfigurationError",
    "HandlerError",
    "ValidationError",
    "RPCError",
    "HTTPError",
    "WebSocketError",
    "TimerError",
    "DatabaseError",
    "PerformanceError",
    "AuthenticationError",
    "AuthorizationError",
    "ErrorHandler",
    
    # Database
    "DatabaseConnection",
    "DatabaseModel", 
    "Repository",
    "SecureRepository",
    "get_db_connection",
    
    # Performance
    "BatchProcessor",
    "OptimizedNATSConnection",
    "PerformanceMetrics",
    
    # Logging
    "LoggingConfig",
    "get_service_logger",
    "LoggingMixin",
    "HTTPLoggingMixin", 
    "WebSocketLoggingMixin",
    
    # Debug
    "BackdoorServer",
    "BackdoorClient",
    
    # Runners
    "ServiceRunner",
    "ServiceOrchestrator",
    
    # Correlation ID support
    "CorrelationContext",
    "get_correlation_id",
    "set_correlation_id", 
    "create_correlation_id",
    "with_correlation_id",
    "CorrelationMiddleware",
    "WebSocketCorrelationMiddleware",
    "correlation_id_dependency",
    "setup_correlation_logging",
    "get_correlation_logger",
    "CorrelationLoggerMixin",
    
    # Auth exports
    "AuthConfig",
    "AuthUser",
    "AuthContext",
    "SimpleAuthService",
    "AuthenticationError",
    "AuthorizationError",
    "requires_auth",
    "requires_roles",
    "requires_permissions",
    "get_current_user",
    "get_current_context",
    "set_auth_service",
    "AuthMiddleware",
]
