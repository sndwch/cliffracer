"""
Cliffracer Debug Module

Provides live debugging capabilities for running services including:
- Interactive Python shell (backdoor) for service inspection
- NATS connection and message debugging
- Service state and performance monitoring
"""

from .backdoor import BackdoorClient, BackdoorServer, is_backdoor_enabled
from .inspector import NATSInspector, ServiceInspector

__all__ = [
    "BackdoorServer",
    "BackdoorClient",
    "ServiceInspector",
    "NATSInspector",
    "is_backdoor_enabled",
]
