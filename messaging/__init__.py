"""
Modular messaging package for NATS microservices framework

Supports multiple messaging backends:
- NATS (default)
- AWS SNS/SQS/EventBridge
- Google Cloud Pub/Sub (planned)
- Azure Service Bus (planned)
- Apache Kafka (planned)
"""

from .abstract_messaging import (
    MessageBroker,
    MessageClient,
    Message,
    MessageConfig,
    MessageDeliveryMode,
    MessagePersistence,
    MessagingConfig,
    MessageClientFactory,
    SubscriptionConfig,
)
from .aws_messaging import AWSMessageBroker, AWSClient

# Import implementations to register them
from .nats_messaging import NATSMessageBroker, NATSClient

__all__ = [
    "MessageClient",
    "MessageBroker",
    "MessageClientFactory", 
    "MessagingConfig",
    "Message",
    "MessageConfig",
    "SubscriptionConfig",
    "MessageDeliveryMode",
    "MessagePersistence",
    "NATSClient",
    "NATSMessageBroker",
    "AWSClient",
    "AWSMessageBroker",
]
