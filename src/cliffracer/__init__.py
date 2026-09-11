"""Cliffracer application framework.

Core runtime for Python services communicating over NATS. Provides RPC,
event pub/sub, scheduled timers, JetStream consumer configuration, and
health endpoints. Extensions provide optional HTTP, auth, metrics, and tracing.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("cliffracer")
except PackageNotFoundError:  # not installed (e.g. running from a raw source tree)
    __version__ = "0.0.0+unknown"

# Core exports - Consolidated Service Architecture
# Note: authentication is the cliffracer-auth distribution: from cliffracer_auth import ...
# The base every generated client extends, and the errors a call can raise.
from cliffracer.client import (
    ClientError,
    ClientOutOfDate,
    ClientOutOfDateError,
    RpcClientError,
    RpcError,
    RpcNoResponders,
    RpcNoRespondersError,
    RpcRefused,
    RpcRefusedError,
    RpcServerError,
    RpcTimeout,
    RpcTimeoutError,
    RpcUnknownMethod,
    RpcUnknownMethodError,
    RpcValidationError,
    ServiceClient,
)

# Correlation ID support
from cliffracer.core.correlation import (
    CorrelationContext,
    create_correlation_id,
    get_correlation_id,
    set_correlation_id,
    with_correlation_id,
)
from cliffracer.core.decorators import (
    async_rpc,
    broadcast,
    idempotent,
    listener,
    rpc,
    timer,
    validated_listener,
)

# Decorator exports - All decorators in one place
from cliffracer.core.dependencies import Dependency, dependency

# Exception hierarchy
from cliffracer.core.exceptions import (
    CliffracerError,
    ConfigurationError,
    ConnectionError,
    ErrorHandler,
    HandlerError,
    IdempotencyKeyError,
    RPCError,
    ServiceError,
    ServiceLifecycleError,
    TimerError,
    ValidationError,
)

# Idempotency support
from cliffracer.core.idempotency import (
    IdempotencyContext,
)

# JetStream stream declaration
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec

# Message types
from cliffracer.core.messages import (
    BroadcastMessage,
    Message,
    RPCRequest,
    RPCResponse,
)
from cliffracer.core.service import (
    CliffracerService,
)

# Configuration
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.timer import Timer

# Invariants
from cliffracer.invariants import override_length_check

# RPC proxy descriptor
from cliffracer.rpc_proxy import RpcProxy

# Runner exports
from cliffracer.runners.orchestrator import ServiceOrchestrator, ServiceRunner

__all__ = [
    # Version
    "__version__",
    # Core Service Classes - Consolidated Architecture
    "CliffracerService",
    # Generated clients: the base and the errors a call can raise
    "ServiceClient",
    "RpcError",
    "RpcClientError",
    "RpcServerError",
    "RpcTimeoutError",
    "RpcNoRespondersError",
    "RpcValidationError",
    "RpcUnknownMethodError",
    "RpcRefusedError",
    "ClientOutOfDateError",
    "ClientError",
    "RpcUnknownMethod",
    "RpcRefused",
    "RpcTimeout",
    "RpcNoResponders",
    "ClientOutOfDate",
    # Configuration
    "ServiceConfig",
    "Timer",
    # Decorators - All in one place
    "rpc",
    "async_rpc",
    "validated_listener",
    "broadcast",
    "listener",
    "timer",
    "idempotent",
    "dependency",
    "Dependency",
    # Message Types
    "Message",
    "RPCRequest",
    "RPCResponse",
    "BroadcastMessage",
    # Exception Hierarchy
    "CliffracerError",
    "ServiceError",
    "ServiceLifecycleError",
    "ConnectionError",
    "ConfigurationError",
    "HandlerError",
    "IdempotencyKeyError",
    "ValidationError",
    "RPCError",
    "TimerError",
    "ErrorHandler",
    # Performance
    # RPC Proxy
    "RpcProxy",
    # Runners
    "ServiceRunner",
    "ServiceOrchestrator",
    # Correlation ID support
    "CorrelationContext",
    "get_correlation_id",
    "set_correlation_id",
    "create_correlation_id",
    "with_correlation_id",
    # Idempotency support
    "IdempotencyContext",
    # JetStream
    "StreamSpec",
    "StreamDeclarationError",
    # Invariants
    "override_length_check",
]
