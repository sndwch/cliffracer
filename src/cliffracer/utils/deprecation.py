"""
Backward compatibility aliases for refactored class names.
These will be removed in a future version.
"""

import warnings
from typing import TYPE_CHECKING

# Import new classes
from nats_service import BaseNATSService
from nats_service import NATSService
from nats_service_extended import ValidatedNATSService
from nats_service_extended import HTTPNATSService  
from nats_service_extended import WebSocketNATSService
from nats_service_extended import ConfigurableNATSService
from nats_service_extended import PluggableNATSService
from nats_service_extended import SecureNATSService
from messaging import NATSClient
from messaging import AWSClient
from messaging import MessageClient
from messaging import MessageClientFactory
from monitoring.metrics import ZabbixMetricsService
from monitoring.clients import MonitoringClient
from monitoring.clients import CloudWatchClient
from nats_service import NATSServiceMeta
from nats_service_extended import ValidatedNATSServiceMeta as ValidatedServiceMeta
from nats_runner import ServiceRunner
from nats_runner import ServiceOrchestrator

# Create deprecated aliases

def _deprecated_natsservice(*args, **kwargs):
    warnings.warn(
        "NatsService is deprecated. Use BaseNATSService instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return BaseNATSService(*args, **kwargs)

NatsService = _deprecated_natsservice

def _deprecated_service(*args, **kwargs):
    warnings.warn(
        "Service is deprecated. Use NATSService instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return NATSService(*args, **kwargs)

Service = _deprecated_service

def _deprecated_extendedservice(*args, **kwargs):
    warnings.warn(
        "ExtendedService is deprecated. Use ValidatedNATSService instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return ValidatedNATSService(*args, **kwargs)

ExtendedService = _deprecated_extendedservice

def _deprecated_httpservice(*args, **kwargs):
    warnings.warn(
        "HTTPService is deprecated. Use HTTPNATSService instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return HTTPNATSService(*args, **kwargs)

HTTPService = _deprecated_httpservice

def _deprecated_websocketservice(*args, **kwargs):
    warnings.warn(
        "WebSocketService is deprecated. Use WebSocketNATSService instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return WebSocketNATSService(*args, **kwargs)

WebSocketService = _deprecated_websocketservice

def _deprecated_modularservice(*args, **kwargs):
    warnings.warn(
        "ModularService is deprecated. Use ConfigurableNATSService instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return ConfigurableNATSService(*args, **kwargs)

ModularService = _deprecated_modularservice

def _deprecated_fullymodularservice(*args, **kwargs):
    warnings.warn(
        "FullyModularService is deprecated. Use PluggableNATSService instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return PluggableNATSService(*args, **kwargs)

FullyModularService = _deprecated_fullymodularservice

def _deprecated_authenticatedservice(*args, **kwargs):
    warnings.warn(
        "AuthenticatedService is deprecated. Use SecureNATSService instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return SecureNATSService(*args, **kwargs)

AuthenticatedService = _deprecated_authenticatedservice

def _deprecated_natsmessagingclient(*args, **kwargs):
    warnings.warn(
        "NATSMessagingClient is deprecated. Use NATSClient instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return NATSClient(*args, **kwargs)

NATSMessagingClient = _deprecated_natsmessagingclient

def _deprecated_awsmessagingclient(*args, **kwargs):
    warnings.warn(
        "AWSMessagingClient is deprecated. Use AWSClient instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return AWSClient(*args, **kwargs)

AWSMessagingClient = _deprecated_awsmessagingclient

def _deprecated_abstractmessagingclient(*args, **kwargs):
    warnings.warn(
        "AbstractMessagingClient is deprecated. Use MessageClient instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return MessageClient(*args, **kwargs)

AbstractMessagingClient = _deprecated_abstractmessagingclient

def _deprecated_abstractmessagebroker(*args, **kwargs):
    warnings.warn(
        "AbstractMessageBroker is deprecated. Use MessageBroker instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return MessageBroker(*args, **kwargs)

AbstractMessageBroker = _deprecated_abstractmessagebroker

def _deprecated_messagingfactory(*args, **kwargs):
    warnings.warn(
        "MessagingFactory is deprecated. Use MessageClientFactory instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return MessageClientFactory(*args, **kwargs)

MessagingFactory = _deprecated_messagingfactory

def _deprecated_metricsexporterservice(*args, **kwargs):
    warnings.warn(
        "MetricsExporterService is deprecated. Use ZabbixMetricsService instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return ZabbixMetricsService(*args, **kwargs)

MetricsExporterService = _deprecated_metricsexporterservice

def _deprecated_abstractmonitoringclient(*args, **kwargs):
    warnings.warn(
        "AbstractMonitoringClient is deprecated. Use MonitoringClient instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return MonitoringClient(*args, **kwargs)

AbstractMonitoringClient = _deprecated_abstractmonitoringclient

def _deprecated_cloudwatchmonitoringclient(*args, **kwargs):
    warnings.warn(
        "CloudWatchMonitoringClient is deprecated. Use CloudWatchClient instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return CloudWatchClient(*args, **kwargs)

CloudWatchMonitoringClient = _deprecated_cloudwatchmonitoringclient

def _deprecated_zabbixsender(*args, **kwargs):
    warnings.warn(
        "ZabbixSender is deprecated. Use ZabbixExporter instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return ZabbixExporter(*args, **kwargs)

ZabbixSender = _deprecated_zabbixsender

def _deprecated_metricscollector(*args, **kwargs):
    warnings.warn(
        "MetricsCollector is deprecated. Use SystemMetricsCollector instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return SystemMetricsCollector(*args, **kwargs)

MetricsCollector = _deprecated_metricscollector

def _deprecated_servicemeta(*args, **kwargs):
    warnings.warn(
        "ServiceMeta is deprecated. Use NATSServiceMeta instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return NATSServiceMeta(*args, **kwargs)

ServiceMeta = _deprecated_servicemeta

def _deprecated_extendedservicemeta(*args, **kwargs):
    warnings.warn(
        "ExtendedServiceMeta is deprecated. Use ValidatedServiceMeta instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return ValidatedServiceMeta(*args, **kwargs)

ExtendedServiceMeta = _deprecated_extendedservicemeta

def _deprecated_abstractservicerunner(*args, **kwargs):
    warnings.warn(
        "AbstractServiceRunner is deprecated. Use ServiceRunner instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return ServiceRunner(*args, **kwargs)

AbstractServiceRunner = _deprecated_abstractservicerunner

def _deprecated_lambdaservicerunner(*args, **kwargs):
    warnings.warn(
        "LambdaServiceRunner is deprecated. Use AWSLambdaRunner instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return AWSLambdaRunner(*args, **kwargs)

LambdaServiceRunner = _deprecated_lambdaservicerunner

def _deprecated_multiservicerunner(*args, **kwargs):
    warnings.warn(
        "MultiServiceRunner is deprecated. Use ServiceOrchestrator instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return ServiceOrchestrator(*args, **kwargs)

MultiServiceRunner = _deprecated_multiservicerunner
