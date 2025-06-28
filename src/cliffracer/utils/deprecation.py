"""
Backward compatibility aliases for refactored class names.
These will be removed in a future version.

WARNING: Many of these classes reference broken or non-existent modules.
See IMPLEMENTATION_STATUS.md for what actually works.
"""

import warnings
from typing import Any


class _DeprecatedClass:
    """Placeholder for deprecated classes that no longer exist"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            f"{self.__class__.__name__} is deprecated and no longer functional. "
            "See IMPLEMENTATION_STATUS.md for current alternatives.",
            DeprecationWarning,
            stacklevel=2,
        )


def _create_deprecated_alias(old_name: str, new_name: str | None = None) -> type[_DeprecatedClass]:
    """Create a deprecated class alias that warns when used"""

    class DeprecatedAlias(_DeprecatedClass):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if new_name:
                message = f"{old_name} is deprecated. Use {new_name} instead."
            else:
                message = f"{old_name} is deprecated and no longer available."

            warnings.warn(message, DeprecationWarning, stacklevel=2)

    DeprecatedAlias.__name__ = old_name
    return DeprecatedAlias


# Messaging aliases (broken imports)
NATSClient = _create_deprecated_alias("NATSClient", "cliffracer.NATSService")
MessageClient = _create_deprecated_alias("MessageClient", "cliffracer.NATSService")
AWSClient = _create_deprecated_alias("AWSClient", None)  # Not implemented
MessageClientFactory = _create_deprecated_alias("MessageClientFactory", None)  # Not implemented

# Monitoring aliases (broken imports)
CloudWatchClient = _create_deprecated_alias("CloudWatchClient", None)  # Not integrated
MonitoringClient = _create_deprecated_alias("MonitoringClient", None)  # Not integrated
ZabbixMetricsService = _create_deprecated_alias("ZabbixMetricsService", None)  # Not integrated


# Service aliases (working replacements available)
def _deprecated_natsservice(*args: Any, **kwargs: Any) -> Any:
    warnings.warn(
        "NatsService is deprecated. Use BaseNATSService instead.", DeprecationWarning, stacklevel=2
    )
    from cliffracer import NATSService

    return NATSService(*args, **kwargs)


def _deprecated_extendedservice(*args: Any, **kwargs: Any) -> Any:
    warnings.warn(
        "ExtendedService is deprecated. Use ValidatedNATSService instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    from cliffracer import ValidatedNATSService

    return ValidatedNATSService(*args, **kwargs)


def _deprecated_httpservice(*args: Any, **kwargs: Any) -> Any:
    warnings.warn(
        "HTTPService is deprecated. Use HTTPNATSService instead.", DeprecationWarning, stacklevel=2
    )
    from cliffracer import HTTPNATSService

    return HTTPNATSService(*args, **kwargs)


def _deprecated_websocketservice(*args: Any, **kwargs: Any) -> Any:
    warnings.warn(
        "WebSocketService is deprecated. Use WebSocketNATSService instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    # WebSocketNATSService not in main exports yet
    warnings.warn(
        "WebSocketNATSService not yet available in main exports", UserWarning, stacklevel=2
    )
    return None


def _deprecated_servicerunner(*args: Any, **kwargs: Any) -> Any:
    warnings.warn(
        "ServiceRunner is deprecated. Use ServiceOrchestrator instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    from cliffracer import ServiceOrchestrator

    return ServiceOrchestrator(*args, **kwargs)


# Create aliases that work (redirect to new classes)
NatsService = _deprecated_natsservice
ExtendedService = _deprecated_extendedservice
HTTPService = _deprecated_httpservice
WebSocketService = _deprecated_websocketservice
ServiceRunner = _deprecated_servicerunner

# Create aliases for broken functionality
ConfigurableNATSService = _create_deprecated_alias(
    "ConfigurableNATSService", "cliffracer.NATSService"
)
PluggableNATSService = _create_deprecated_alias("PluggableNATSService", None)  # Not implemented
SecureNATSService = _create_deprecated_alias("SecureNATSService", None)  # Not implemented

# Metaclass aliases
BaseNATSServiceMeta = _create_deprecated_alias("BaseNATSServiceMeta", None)
ValidatedServiceMeta = _create_deprecated_alias("ValidatedServiceMeta", None)


# Service orchestration aliases that work
def _deprecated_serviceorchestrator(*args: Any, **kwargs: Any) -> Any:
    warnings.warn(
        "Importing ServiceOrchestrator from deprecation module is deprecated. "
        "Import directly from cliffracer instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    from cliffracer import ServiceOrchestrator

    return ServiceOrchestrator(*args, **kwargs)


ServiceOrchestrator = _deprecated_serviceorchestrator

# Define what names are available for import
deprecated_names: dict[str, str | None] = {
    # Working replacements
    "NatsService": "cliffracer.NATSService",
    "ExtendedService": "cliffracer.ValidatedNATSService",
    "HTTPService": "cliffracer.HTTPNATSService",
    "WebSocketService": "cliffracer.WebSocketNATSService",
    "ServiceRunner": "cliffracer.ServiceOrchestrator",
    "ServiceOrchestrator": "cliffracer.ServiceOrchestrator",
    # Non-working (broken/not implemented)
    "NATSClient": None,
    "MessageClient": None,
    "AWSClient": None,
    "MessageClientFactory": None,
    "CloudWatchClient": None,
    "MonitoringClient": None,
    "ZabbixMetricsService": None,
    "ConfigurableNATSService": None,
    "PluggableNATSService": None,
    "SecureNATSService": None,
}


def get_replacement(deprecated_name: str) -> str | None:
    """Get the recommended replacement for a deprecated class name"""
    return deprecated_names.get(deprecated_name)


def list_deprecated() -> list[str]:
    """List all deprecated class names"""
    return list(deprecated_names.keys())


def list_working_replacements() -> dict[str, str]:
    """List deprecated names that have working replacements"""
    return {name: replacement for name, replacement in deprecated_names.items() if replacement}


def list_broken() -> list[str]:
    """List deprecated names that have no working replacement"""
    return [name for name, replacement in deprecated_names.items() if replacement is None]
