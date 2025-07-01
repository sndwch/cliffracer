"""
Abstract messaging layer for modular backend support
Allows switching between NATS, AWS SNS/SQS, Google Pub/Sub, etc.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class MessageDeliveryMode(str, Enum):
    """Message delivery guarantees"""

    AT_MOST_ONCE = "at_most_once"  # Fire and forget
    AT_LEAST_ONCE = "at_least_once"  # Guaranteed delivery, possible duplicates
    EXACTLY_ONCE = "exactly_once"  # Guaranteed delivery, no duplicates


class MessagePersistence(str, Enum):
    """Message persistence options"""

    MEMORY = "memory"  # In-memory, fast but not durable
    PERSISTENT = "persistent"  # Durable storage
    REPLICATED = "replicated"  # Replicated across multiple nodes


@dataclass
class MessageConfig:
    """Configuration for message handling"""

    delivery_mode: MessageDeliveryMode = MessageDeliveryMode.AT_LEAST_ONCE
    persistence: MessagePersistence = MessagePersistence.PERSISTENT
    ttl_seconds: int | None = None
    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    dead_letter_enabled: bool = True


@dataclass
class Message:
    """Message representation for the messaging interface"""
    
    subject: str
    data: bytes
    headers: dict[str, str] | None = None
    reply_subject: str | None = None
    timestamp: float | None = None
    correlation_id: str | None = None


@dataclass
class SubscriptionConfig:
    """Configuration for subscriptions"""

    subject: str
    queue_group: str | None = None  # For load balancing
    durable_name: str | None = None  # For durable subscriptions
    auto_ack: bool = True
    max_pending: int = 1000
    ack_wait_seconds: float = 30.0


class MessageClient(ABC):
    """Abstract base class for messaging clients"""

    @abstractmethod
    async def connect(self, **kwargs) -> None:
        """Connect to the messaging system"""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the messaging system"""
        pass

    @abstractmethod
    async def publish(
        self,
        subject: str,
        data: bytes,
        headers: dict[str, str] | None = None,
        config: MessageConfig | None = None,
    ) -> None:
        """Publish a message"""
        pass

    @abstractmethod
    async def request(
        self,
        subject: str,
        data: bytes,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> Any:  # Returns Message object when implemented
        """Send request and wait for response"""
        pass

    @abstractmethod
    async def subscribe(
        self, config: SubscriptionConfig, callback: Callable[[Any], Any]  # Message object when implemented
    ) -> str:
        """Subscribe to messages"""
        pass

    @abstractmethod
    async def unsubscribe(self, subscription_id: str) -> None:
        """Unsubscribe from messages"""
        pass

    @abstractmethod
    async def create_stream(
        self, name: str, subjects: list[str], config: MessageConfig | None = None
    ) -> None:
        """Create a persistent message stream"""
        pass

    @abstractmethod
    async def delete_stream(self, name: str) -> None:
        """Delete a message stream"""
        pass

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        """Get messaging system statistics"""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connected to messaging system"""
        pass


class MessageBroker(ABC):
    """Abstract message broker for higher-level operations"""

    def __init__(self, client: MessageClient):
        self.client = client
        self._subscriptions: dict[str, str] = {}
        self._streams: dict[str, Any] = {}

    @abstractmethod
    async def setup_rpc_pattern(self, service_name: str) -> None:
        """Setup RPC request-response pattern"""
        pass

    @abstractmethod
    async def setup_pubsub_pattern(self, topics: list[str]) -> None:
        """Setup publish-subscribe pattern"""
        pass

    @abstractmethod
    async def setup_queue_pattern(self, queues: list[str]) -> None:
        """Setup queue-based message processing"""
        pass

    async def call_rpc(self, service: str, method: str, timeout: float = 30.0, **kwargs) -> Any:
        """Call remote procedure"""
        subject = f"{service}.{method}"
        data = json.dumps(kwargs).encode()

        response = await self.client.request(subject, data, timeout)
        return json.loads(response.data.decode())

    async def call_async(self, service: str, method: str, **kwargs) -> None:
        """Call remote procedure asynchronously (fire-and-forget)"""
        subject = f"{service}.async.{method}"
        data = json.dumps(kwargs).encode()

        await self.client.publish(subject, data)

    async def publish_event(self, subject: str, **kwargs) -> None:
        """Publish an event"""
        data = json.dumps(kwargs).encode()
        await self.client.publish(subject, data)

    async def subscribe_to_events(
        self, pattern: str, callback: Callable[[str, dict[str, Any]], Any]
    ) -> str:
        """Subscribe to events matching pattern"""

        async def message_handler(msg: Any):  # Message object when implemented
            try:
                data = json.loads(msg.data.decode()) if msg.data else {}
                await callback(msg.subject, data)
            except Exception as e:
                print(f"Error handling message: {e}")

        config = SubscriptionConfig(subject=pattern)
        return await self.client.subscribe(config, message_handler)


class MessageClientFactory:
    """Factory for creating messaging clients"""

    _clients: dict[str, type] = {}

    @classmethod
    def register_client(cls, name: str, client_class: type):
        """Register a messaging client implementation"""
        cls._clients[name] = client_class

    @classmethod
    def create_client(cls, backend: str, **config) -> MessageClient:
        """Create a messaging client instance"""
        if backend not in cls._clients:
            raise ValueError(f"Unknown messaging backend: {backend}")

        return cls._clients[backend](**config)

    @classmethod
    def list_backends(cls) -> list[str]:
        """List available messaging backends"""
        return list(cls._clients.keys())


class MessagingConfig:
    """Configuration for messaging systems"""

    def __init__(
        self,
        backend: str,
        connection_params: dict[str, Any],
        default_message_config: MessageConfig | None = None,
    ):
        self.backend = backend
        self.connection_params = connection_params
        self.default_message_config = default_message_config or MessageConfig()

    @classmethod
    def nats(cls, url: str = "nats://localhost:4222", **kwargs) -> "MessagingConfig":
        """Create NATS configuration"""
        return cls(backend="nats", connection_params={"url": url, **kwargs})

    @classmethod
    def aws_sns_sqs(
        cls,
        region: str = "us-east-1",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        **kwargs,
    ) -> "MessagingConfig":
        """Create AWS SNS/SQS configuration"""
        return cls(
            backend="aws",
            connection_params={
                "region": region,
                "access_key_id": access_key_id,
                "secret_access_key": secret_access_key,
                **kwargs,
            },
        )

    @classmethod
    def google_pubsub(
        cls, project_id: str, credentials_path: str | None = None, **kwargs
    ) -> "MessagingConfig":
        """Create Google Pub/Sub configuration"""
        return cls(
            backend="google",
            connection_params={
                "project_id": project_id,
                "credentials_path": credentials_path,
                **kwargs,
            },
        )

    @classmethod
    def azure_service_bus(cls, connection_string: str, **kwargs) -> "MessagingConfig":
        """Create Azure Service Bus configuration"""
        return cls(
            backend="azure", connection_params={"connection_string": connection_string, **kwargs}
        )


# Decorators for messaging patterns
def with_messaging_client(config_key: str = "messaging"):
    """Decorator to inject messaging client"""

    def decorator(cls):
        original_init = cls.__init__

        def new_init(self, *args, **kwargs):
            # Extract messaging config
            messaging_config = kwargs.pop(config_key, None)
            if messaging_config:
                self.messaging_client = MessageClientFactory.create_client(
                    messaging_config.backend, **messaging_config.connection_params
                )
                self.messaging_broker = MessageBroker(self.messaging_client)

            original_init(self, *args, **kwargs)

        cls.__init__ = new_init
        return cls

    return decorator
