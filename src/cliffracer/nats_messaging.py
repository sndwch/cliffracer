"""
NATS implementation of the abstract messaging interface
Wraps existing NATS functionality to conform to the modular interface
"""

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import nats
from nats.js import JetStreamContext

from .abstract_messaging import (
    Message,
    MessageBroker,
    MessageClient,
    MessageClientFactory,
    MessageConfig,
    MessagePersistence,
    SubscriptionConfig,
)


class NATSClient(MessageClient):
    """NATS implementation of the messaging client"""

    def __init__(
        self,
        url: str = "nats://localhost:4222",
        user: str | None = None,
        password: str | None = None,
        token: str | None = None,
        **kwargs,
    ):
        self.url = url
        self.user = user
        self.password = password
        self.token = token
        self.connect_kwargs = kwargs

        self.nc: nats.NATS | None = None
        self.js: JetStreamContext | None = None
        self._subscriptions: dict[str, Any] = {}
        self._streams: dict[str, Any] = {}

    async def connect(self, **kwargs) -> None:
        """Connect to NATS server"""
        if self.nc and not self.nc.is_closed:
            return

        connect_params = {"servers": [self.url], **self.connect_kwargs, **kwargs}

        if self.user and self.password:
            connect_params["user"] = self.user
            connect_params["password"] = self.password
        elif self.token:
            connect_params["token"] = self.token

        try:
            self.nc = await nats.connect(**connect_params)
            self.js = self.nc.jetstream()
            print(f"Connected to NATS at {self.url}")

        except Exception as e:
            raise ConnectionError(f"Failed to connect to NATS: {e}")

    async def disconnect(self) -> None:
        """Disconnect from NATS"""
        if self.nc and not self.nc.is_closed:
            await self.nc.close()

        self._subscriptions.clear()
        self._streams.clear()
        print("Disconnected from NATS")

    async def publish(
        self,
        subject: str,
        data: bytes,
        headers: dict[str, str] | None = None,
        config: MessageConfig | None = None,
    ) -> None:
        """Publish message to NATS"""
        if not self.nc or self.nc.is_closed:
            raise RuntimeError("Not connected to NATS")

        config = config or MessageConfig()

        # Convert headers
        nats_headers = None
        if headers:
            nats_headers = {}
            for key, value in headers.items():
                nats_headers[key] = str(value)

        try:
            if config.persistence == MessagePersistence.PERSISTENT and self.js:
                # Use JetStream for persistent messages
                await self.js.publish(subject, data, headers=nats_headers)
            else:
                # Use core NATS for non-persistent messages
                await self.nc.publish(subject, data, headers=nats_headers)

        except Exception as e:
            raise RuntimeError(f"Failed to publish to NATS: {e}")

    async def request(
        self,
        subject: str,
        data: bytes,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> Message:
        """Send request and wait for response"""
        if not self.nc or self.nc.is_closed:
            raise RuntimeError("Not connected to NATS")

        # Convert headers
        nats_headers = None
        if headers:
            nats_headers = {}
            for key, value in headers.items():
                nats_headers[key] = str(value)

        try:
            response = await self.nc.request(subject, data, timeout=timeout, headers=nats_headers)

            # Convert NATS message to our Message format
            response_headers = {}
            if response.headers:
                response_headers = dict(response.headers)

            return Message(
                subject=response.subject,
                data=response.data,
                headers=response_headers,
                reply_subject=response.reply,
            )

        except Exception as e:
            raise RuntimeError(f"NATS request failed: {e}")

    async def subscribe(
        self, config: SubscriptionConfig, callback: Callable[[Message], Any]
    ) -> str:
        """Subscribe to NATS subject"""
        if not self.nc or self.nc.is_closed:
            raise RuntimeError("Not connected to NATS")

        subscription_id = str(uuid.uuid4())

        async def message_handler(msg):
            # Convert NATS message to our Message format
            headers = {}
            if msg.headers:
                headers = dict(msg.headers)

            message = Message(
                subject=msg.subject, data=msg.data, headers=headers, reply_subject=msg.reply
            )

            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(message)
                else:
                    callback(message)
            except Exception as e:
                print(f"Error in message handler: {e}")

        try:
            if config.durable_name and self.js:
                # Use JetStream durable consumer
                if config.subject not in self._streams:
                    # Create stream if it doesn't exist
                    try:
                        await self.js.add_stream(
                            name=f"stream_{config.subject.replace('.', '_').replace('*', 'wildcard')}",
                            subjects=[config.subject],
                        )
                    except Exception:
                        pass  # Stream might already exist

                subscription = await self.js.subscribe(
                    config.subject,
                    cb=message_handler,
                    durable=config.durable_name,
                    queue=config.queue_group,
                )
            else:
                # Use core NATS subscription
                subscription = await self.nc.subscribe(
                    config.subject, cb=message_handler, queue=config.queue_group
                )

            self._subscriptions[subscription_id] = {"subscription": subscription, "config": config}

            return subscription_id

        except Exception as e:
            raise RuntimeError(f"Failed to subscribe to NATS: {e}")

    async def unsubscribe(self, subscription_id: str) -> None:
        """Unsubscribe from NATS subject"""
        if subscription_id in self._subscriptions:
            sub_info = self._subscriptions[subscription_id]
            await sub_info["subscription"].unsubscribe()
            del self._subscriptions[subscription_id]

    async def create_stream(
        self, name: str, subjects: list[str], config: MessageConfig | None = None
    ) -> None:
        """Create JetStream stream"""
        if not self.js:
            raise RuntimeError("JetStream not available")

        config = config or MessageConfig()

        # Configure stream based on message config
        stream_config = {"name": name, "subjects": subjects}

        if config.persistence == MessagePersistence.MEMORY:
            stream_config["storage"] = "memory"
        else:
            stream_config["storage"] = "file"

        if config.ttl_seconds:
            stream_config["max_age"] = config.ttl_seconds

        try:
            stream = await self.js.add_stream(**stream_config)
            self._streams[name] = stream
            print(f"Created NATS stream: {name}")

        except Exception as e:
            raise RuntimeError(f"Failed to create NATS stream: {e}")

    async def delete_stream(self, name: str) -> None:
        """Delete JetStream stream"""
        if not self.js:
            raise RuntimeError("JetStream not available")

        try:
            await self.js.delete_stream(name)
            if name in self._streams:
                del self._streams[name]
            print(f"Deleted NATS stream: {name}")

        except Exception as e:
            raise RuntimeError(f"Failed to delete NATS stream: {e}")

    async def get_stats(self) -> dict[str, Any]:
        """Get NATS statistics"""
        stats = {
            "backend": "nats",
            "url": self.url,
            "connected": self.is_connected,
            "subscriptions": len(self._subscriptions),
            "streams": len(self._streams),
        }

        if self.nc and not self.nc.is_closed:
            # Get server info
            try:
                server_info = self.nc._server_info
                stats["server_info"] = {
                    "server_id": server_info.get("server_id"),
                    "version": server_info.get("version"),
                    "max_payload": server_info.get("max_payload"),
                }
            except Exception:
                pass

        if self.js:
            # Get JetStream account info
            try:
                account_info = await self.js.account_info()
                stats["jetstream"] = {
                    "memory": account_info.memory,
                    "storage": account_info.storage,
                    "streams": account_info.streams,
                    "consumers": account_info.consumers,
                }
            except Exception:
                pass

        return stats

    @property
    def is_connected(self) -> bool:
        return self.nc is not None and not self.nc.is_closed


class NATSMessageBroker(MessageBroker):
    """NATS-specific message broker"""

    async def setup_rpc_pattern(self, service_name: str) -> None:
        """Setup RPC pattern - no special setup needed for NATS"""
        # NATS handles RPC patterns natively
        pass

    async def setup_pubsub_pattern(self, topics: list[str]) -> None:
        """Setup pub/sub pattern - no special setup needed for NATS"""
        # NATS handles pub/sub natively
        pass

    async def setup_queue_pattern(self, queues: list[str]) -> None:
        """Setup queue pattern using JetStream"""
        if hasattr(self.client, "js") and self.client.js:
            for queue in queues:
                try:
                    await self.client.create_stream(
                        name=f"queue_{queue}", subjects=[f"queue.{queue}"]
                    )
                except Exception as e:
                    print(f"Warning: Could not create queue stream for {queue}: {e}")


# Register NATS client with factory
MessageClientFactory.register_client("nats", NATSClient)
