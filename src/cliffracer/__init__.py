"""Cliffracer application framework.

Core runtime for Python services communicating over NATS. Provides RPC,
event pub/sub, scheduled timers, JetStream consumer configuration, and
health endpoints. Extensions provide optional HTTP, auth, metrics, and tracing.
"""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("cliffracer")
except _PackageNotFoundError:  # not installed (e.g. running from a raw source tree)
    __version__ = "0.0.0+unknown"

# Core exports - Consolidated Service Architecture
# Note: authentication is the cliffracer-auth distribution: from cliffracer_auth import ...
# The base every generated client extends, and the errors a call can raise.
from cliffracer.client import (
    ClientError,
    ClientOutOfDate,
    ClientOutOfDateError,
    RpcBusyError,
    RpcClientError,
    RpcConnectionError,
    RpcDeadlineExceededError,
    RpcError,
    RpcNoResponders,
    RpcNoRespondersError,
    RpcRefused,
    RpcRefusedError,
    RpcServerError,
    RpcStreamGapError,
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
    ErrorHandler,
    IdempotencyKeyError,
    RPCError,
    ServiceError,
    ServiceLifecycleError,
    ValidationError,
)

# The extension contract
from cliffracer.core.extension import (
    Extension,
    ExtensionIsolationError,
    ExtensionSetupContext,
    RejectMessage,
    RetryMessage,
    SharedDependency,
    WorkerContext,
)

# Idempotency support
from cliffracer.core.idempotency import (
    IdempotencyContext,
)

# JetStream stream declaration
from cliffracer.core.jetstream import (
    ConsumerBindingError,
    MessageScheduleError,
    StreamDeclarationError,
    StreamSpec,
)

# Message types
from cliffracer.core.messages import (
    BroadcastMessage,
    Message,
    RPCRequest,
    RPCResponse,
)
from cliffracer.core.outputs import Output, OutputError
from cliffracer.core.service import (
    CliffracerService,
)

# Configuration
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.timer import Timer

# RPC proxy descriptor
from cliffracer.rpc_proxy import RpcProxy

# Runner exports
from cliffracer.runners.orchestrator import ServiceOrchestrator, ServiceRunner

__all__ = [
    # Version
    "__version__",
    # Core Service Classes - Consolidated Architecture
    "CliffracerService",
    "Output",
    "OutputError",
    # Generated clients: the base and the errors a call can raise
    "ServiceClient",
    "RpcError",
    "RpcClientError",
    "RpcServerError",
    "RpcStreamGapError",
    "RpcTimeoutError",
    "RpcDeadlineExceededError",
    "RpcBusyError",
    "RpcConnectionError",
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
    "ConfigurationError",
    "IdempotencyKeyError",
    "ValidationError",
    "RPCError",
    "ErrorHandler",
    # Extensions
    "Extension",
    "ExtensionIsolationError",
    "ExtensionSetupContext",
    "RejectMessage",
    "RetryMessage",
    "SharedDependency",
    "WorkerContext",
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
    "MessageScheduleError",
    "ConsumerBindingError",
]
