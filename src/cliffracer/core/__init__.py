"""
Core Cliffracer service implementations
"""

from .base_service import BaseNATSService, NATSService, NATSServiceMeta, async_rpc, rpc
from .extended_service import (
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
from .service_config import ServiceConfig

__all__ = [
    # Base classes
    "BaseNATSService",
    "NATSService",
    "NATSServiceMeta",
    "ServiceConfig",
    # Extended classes
    "ValidatedNATSService",
    "HTTPNATSService",
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
]
