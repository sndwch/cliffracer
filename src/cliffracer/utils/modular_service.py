"""
Modular service implementation that can use different messaging backends
Supports NATS, AWS SNS/SQS, Google Pub/Sub, Azure Service Bus, etc.
"""
import asyncio
import inspect
import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Type
from dataclasses import dataclass, field

from messaging.abstract_messaging import (
    MessageClient, AbstractMessageBroker, MessagingFactory,
    MessagingConfig, Message, MessageConfig, SubscriptionConfig
)
from messaging.nats_messaging import NATSMessageBroker
from messaging.aws_messaging import AWSMessageBroker


logger = logging.getLogger(__name__)


@dataclass
class ConfigurableNATSServiceConfig:
    """Configuration for modular services"""
    name: str
    messaging: MessagingConfig
    auto_restart: bool = True
    request_timeout: float = 30.0
    max_pending_requests: int = 1000
    health_check_interval: float = 30.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class ConfigurableNATSService:
    """Base service class that can use different messaging backends"""
    
    def __init__(self, config: ModularServiceConfig):
        self.config = config
        self.logger = logging.getLogger(f"{config.name}")
        
        # Create messaging client based on config
        self.messaging_client = MessagingFactory.create_client(
            config.messaging.backend,
            **config.messaging.connection_params
        )
        
        # Create appropriate message broker
        self.broker = self._create_broker()
        
        # Service state
        self._running = False
        self._handlers: Dict[str, Callable] = {}
        self._rpc_handlers: Dict[str, Callable] = {}
        self._event_handlers: Dict[str, Callable] = {}
        self._subscriptions: List[str] = []
        
        # Discover handlers from decorators
        self._discover_handlers()
    
    def _create_broker(self) -> MessageBroker:
        """Create appropriate message broker for the backend"""
        backend = self.config.messaging.backend
        
        if backend == "nats":
            return NATSMessageBroker(self.messaging_client)
        elif backend == "aws":
            return AWSMessageBroker(self.messaging_client)
        else:
            # Generic broker for other backends
            return MessageBroker(self.messaging_client)
    
    def _discover_handlers(self):
        """Discover RPC and event handlers from method decorators"""
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            
            if hasattr(attr, '_is_rpc'):
                self._rpc_handlers[attr._rpc_name] = attr
                self.logger.debug(f"Registered RPC handler: {attr._rpc_name}")
            
            elif hasattr(attr, '_is_event_handler'):
                self._event_handlers[attr._event_pattern] = attr
                self.logger.debug(f"Registered event handler: {attr._event_pattern}")\n    \n    async def start(self):\n        \"\"\"Start the service\"\"\"\n        if self._running:\n            return\n        \n        try:\n            # Connect to messaging system\n            await self.messaging_client.connect()\n            \n            # Setup messaging patterns\n            await self.broker.setup_rpc_pattern(self.config.name)\n            \n            # Subscribe to RPC requests\n            if self._rpc_handlers:\n                await self._setup_rpc_subscriptions()\n            \n            # Subscribe to events\n            if self._event_handlers:\n                await self._setup_event_subscriptions()\n            \n            # Call service-specific startup\n            await self.on_startup()\n            \n            self._running = True\n            self.logger.info(f\"Service '{self.config.name}' started with {self.config.messaging.backend} backend\")\n            \n        except Exception as e:\n            self.logger.error(f\"Failed to start service: {e}\")\n            raise\n    \n    async def stop(self):\n        \"\"\"Stop the service\"\"\"\n        if not self._running:\n            return\n        \n        try:\n            # Unsubscribe from all subscriptions\n            for sub_id in self._subscriptions:\n                try:\n                    await self.messaging_client.unsubscribe(sub_id)\n                except Exception as e:\n                    self.logger.warning(f\"Error unsubscribing {sub_id}: {e}\")\n            \n            self._subscriptions.clear()\n            \n            # Call service-specific shutdown\n            await self.on_shutdown()\n            \n            # Disconnect from messaging system\n            await self.messaging_client.disconnect()\n            \n            self._running = False\n            self.logger.info(f\"Service '{self.config.name}' stopped\")\n            \n        except Exception as e:\n            self.logger.error(f\"Error stopping service: {e}\")\n    \n    async def _setup_rpc_subscriptions(self):\n        \"\"\"Subscribe to RPC requests\"\"\"\n        for method_name in self._rpc_handlers:\n            subject = f\"{self.config.name}.{method_name}\"\n            \n            config = SubscriptionConfig(\n                subject=subject,\n                queue_group=self.config.name  # Load balancing\n            )\n            \n            sub_id = await self.messaging_client.subscribe(\n                config,\n                self._handle_rpc_request\n            )\n            \n            self._subscriptions.append(sub_id)\n            self.logger.debug(f\"Subscribed to RPC: {subject}\")\n    \n    async def _setup_event_subscriptions(self):\n        \"\"\"Subscribe to events\"\"\"\n        for pattern in self._event_handlers:\n            config = SubscriptionConfig(\n                subject=pattern,\n                queue_group=self.config.name  # Multiple instances can handle events\n            )\n            \n            sub_id = await self.messaging_client.subscribe(\n                config,\n                self._handle_event\n            )\n            \n            self._subscriptions.append(sub_id)\n            self.logger.debug(f\"Subscribed to events: {pattern}\")\n    \n    async def _handle_rpc_request(self, message: Message):\n        \"\"\"Handle incoming RPC requests\"\"\"\n        method_name = message.subject.split('.')[-1]\n        handler = self._rpc_handlers.get(method_name)\n        \n        if not handler:\n            error_response = {\n                \"error\": f\"Unknown method: {method_name}\",\n                \"timestamp\": datetime.utcnow().isoformat()\n            }\n            \n            if message.reply_subject:\n                await self.messaging_client.publish(\n                    message.reply_subject,\n                    json.dumps(error_response).encode()\n                )\n            return\n        \n        try:\n            # Parse request data\n            data = json.loads(message.data.decode()) if message.data else {}\n            \n            # Call handler\n            if inspect.iscoroutinefunction(handler):\n                result = await handler(**data)\n            else:\n                result = handler(**data)\n            \n            # Send response\n            response = {\n                \"result\": result,\n                \"timestamp\": datetime.utcnow().isoformat()\n            }\n            \n            if message.reply_subject:\n                await self.messaging_client.publish(\n                    message.reply_subject,\n                    json.dumps(response).encode()\n                )\n                \n        except Exception as e:\n            self.logger.error(f\"Error handling RPC {method_name}: {e}\")\n            \n            error_response = {\n                \"error\": str(e),\n                \"timestamp\": datetime.utcnow().isoformat()\n            }\n            \n            if message.reply_subject:\n                await self.messaging_client.publish(\n                    message.reply_subject,\n                    json.dumps(error_response).encode()\n                )\n    \n    async def _handle_event(self, message: Message):\n        \"\"\"Handle incoming events\"\"\"\n        for pattern, handler in self._event_handlers.items():\n            if self._subject_matches(pattern, message.subject):\n                try:\n                    data = json.loads(message.data.decode()) if message.data else {}\n                    \n                    if inspect.iscoroutinefunction(handler):\n                        await handler(subject=message.subject, **data)\n                    else:\n                        handler(subject=message.subject, **data)\n                        \n                except Exception as e:\n                    self.logger.error(f\"Error handling event {message.subject}: {e}\")\n    \n    def _subject_matches(self, pattern: str, subject: str) -> bool:\n        \"\"\"Check if subject matches pattern (supports wildcards)\"\"\"\n        pattern_parts = pattern.split('.')\n        subject_parts = subject.split('.')\n        \n        if len(pattern_parts) != len(subject_parts) and '>' not in pattern:\n            return False\n        \n        for i, (p, s) in enumerate(zip(pattern_parts, subject_parts)):\n            if p == '>':\n                return True\n            elif p == '*':\n                continue\n            elif p != s:\n                return False\n        \n        return True\n    \n    # Service lifecycle hooks\n    \n    async def on_startup(self):\n        \"\"\"Called when service starts - override in subclasses\"\"\"\n        pass\n    \n    async def on_shutdown(self):\n        \"\"\"Called when service stops - override in subclasses\"\"\"\n        pass\n    \n    # Messaging operations\n    \n    async def call_rpc(\n        self, \n        service: str, \n        method: str, \n        timeout: float = None, \n        **kwargs\n    ) -> Any:\n        \"\"\"Call remote procedure\"\"\"\n        timeout = timeout or self.config.request_timeout\n        return await self.broker.call_rpc(service, method, timeout, **kwargs)\n    \n    async def call_async(self, service: str, method: str, **kwargs) -> None:\n        \"\"\"Call remote procedure asynchronously (fire-and-forget)\"\"\"\n        await self.broker.call_async(service, method, **kwargs)\n    \n    async def publish_event(self, subject: str, **kwargs) -> None:\n        \"\"\"Publish an event\"\"\"\n        await self.broker.publish_event(subject, **kwargs)\n    \n    async def get_stats(self) -> Dict[str, Any]:\n        \"\"\"Get service and messaging statistics\"\"\"\n        messaging_stats = await self.messaging_client.get_stats()\n        \n        service_stats = {\n            \"service_name\": self.config.name,\n            \"running\": self._running,\n            \"rpc_handlers\": len(self._rpc_handlers),\n            \"event_handlers\": len(self._event_handlers),\n            \"subscriptions\": len(self._subscriptions),\n            \"messaging\": messaging_stats\n        }\n        \n        return service_stats\n    \n    @property\n    def is_running(self) -> bool:\n        return self._running\n    \n    @property\n    def is_connected(self) -> bool:\n        return self.messaging_client.is_connected\n\n\n# Decorators for the modular service\n\ndef rpc(func: Callable) -> Callable:\n    \"\"\"Mark method as RPC handler\"\"\"\n    func._is_rpc = True\n    func._rpc_name = func.__name__\n    return func\n\n\ndef event_handler(pattern: str):\n    \"\"\"Mark method as event handler\"\"\"\n    def decorator(func: Callable) -> Callable:\n        func._is_event_handler = True\n        func._event_pattern = pattern\n        return func\n    return decorator\n\n\n# HTTP Service with modular messaging\nclass ModularHTTPService(ConfigurableNATSService):\n    \"\"\"HTTP service that uses modular messaging backend\"\"\"\n    \n    def __init__(self, config: ModularServiceConfig, host: str = \"0.0.0.0\", port: int = 8000):\n        super().__init__(config)\n        self.host = host\n        self.port = port\n        \n        # Import FastAPI here to make it optional\n        try:\n            from fastapi import FastAPI\n            import uvicorn\n            self.FastAPI = FastAPI\n            self.uvicorn = uvicorn\n        except ImportError:\n            raise ImportError(\"FastAPI and uvicorn required for HTTP services\")\n        \n        self.app = self.FastAPI(title=f\"{config.name} API\")\n        self._server = None\n        self._setup_routes()\n    \n    def _setup_routes(self):\n        \"\"\"Setup default HTTP routes\"\"\"\n        @self.app.get(\"/health\")\n        async def health():\n            stats = await self.get_stats()\n            return {\n                \"status\": \"healthy\" if self.is_running else \"stopped\",\n                \"service\": self.config.name,\n                \"messaging_backend\": self.config.messaging.backend,\n                \"messaging_connected\": self.is_connected,\n                \"stats\": stats\n            }\n        \n        @self.app.get(\"/info\")\n        async def info():\n            return {\n                \"service\": self.config.name,\n                \"messaging_backend\": self.config.messaging.backend,\n                \"rpc_methods\": list(self._rpc_handlers.keys()),\n                \"event_handlers\": list(self._event_handlers.keys())\n            }\n    \n    async def start(self):\n        \"\"\"Start both messaging and HTTP server\"\"\"\n        await super().start()\n        \n        # Start FastAPI server\n        config = self.uvicorn.Config(\n            app=self.app,\n            host=self.host,\n            port=self.port,\n            log_level=\"info\"\n        )\n        self._server = self.uvicorn.Server(config)\n        \n        # Run server in background\n        asyncio.create_task(self._server.serve())\n        self.logger.info(f\"HTTP server started on {self.host}:{self.port}\")\n    \n    async def stop(self):\n        \"\"\"Stop both messaging and HTTP server\"\"\"\n        if self._server:\n            self._server.should_exit = True\n        \n        await super().stop()\n    \n    # FastAPI route decorators\n    def get(self, *args, **kwargs):\n        return self.app.get(*args, **kwargs)\n    \n    def post(self, *args, **kwargs):\n        return self.app.post(*args, **kwargs)\n    \n    def put(self, *args, **kwargs):\n        return self.app.put(*args, **kwargs)\n    \n    def delete(self, *args, **kwargs):\n        return self.app.delete(*args, **kwargs)