"""
Example: Using LoggingMixin for Service Logging
==============================================

This example demonstrates the new consolidated logging approach using mixins
instead of separate logged service classes.
"""

import asyncio

from cliffracer import (
    HTTPLoggingMixin,
    HTTPNATSService,
    LoggingMixin,
    ServiceConfig,
    ValidatedNATSService,
    WebSocketLoggingMixin,
    WebSocketNATSService,
    rpc,
    websocket_handler,
)


# Example 1: Basic service with logging
class LoggedService(LoggingMixin, ValidatedNATSService):
    """Service with comprehensive logging using mixin"""

    @rpc
    async def process_data(self, data: str) -> dict:
        """Process some data"""
        # Logging is automatic!
        result = {"processed": data.upper(), "length": len(data)}
        return result


# Example 2: HTTP service with logging
class LoggedAPIService(HTTPLoggingMixin, HTTPNATSService):
    """HTTP API service with request/response logging"""

    def __init__(self):
        config = ServiceConfig(
            name="logged_api_service",
            description="API service with automatic HTTP logging"
        )
        super().__init__(config, port=8080)

        # Add HTTP routes
        @self.get("/process/{text}")
        async def process_text(text: str):
            # HTTP requests are automatically logged!
            return {"result": text.upper()}


# Example 3: WebSocket service with logging
class LoggedRealtimeService(WebSocketLoggingMixin, WebSocketNATSService):
    """WebSocket service with connection and broadcast logging"""

    def __init__(self):
        config = ServiceConfig(
            name="logged_realtime_service",
            description="Real-time service with WebSocket logging"
        )
        super().__init__(config, port=8081)

    @websocket_handler("/ws")
    async def handle_connection(self, websocket):
        """Handle WebSocket connections with automatic logging"""
        await websocket.send_json({"message": "Connected!"})

        # Broadcast to all clients - automatically logged!
        await self.broadcast_to_websockets({
            "type": "notification",
            "message": "New client connected",
        })


# Example 4: Custom service combining multiple mixins
class UltimateLoggedService(
    WebSocketLoggingMixin,  # Must come before WebSocketNATSService
    WebSocketNATSService    # Base service class
):
    """
    The ultimate logged service with all features!

    Note: Mixin order matters! LoggingMixin should come before the service class.
    """

    def __init__(self):
        config = ServiceConfig(
            name="ultimate_service",
            description="Service with all logging features"
        )
        super().__init__(config, port=8082)

        # The logger is automatically set up with context
        self.logger.info("UltimateLoggedService initialized!")

    @rpc
    async def do_something(self, action: str) -> str:
        """RPC method - automatically logged"""
        self.logger.debug(f"Performing action: {action}")
        return f"Completed: {action}"

    @websocket_handler("/ws/ultimate")
    async def ultimate_handler(self, websocket):
        """WebSocket handler - connections logged automatically"""
        await websocket.send_json({
            "service": "ultimate",
            "status": "connected"
        })


async def main():
    """Demonstrate the logging mixins"""
    print("🚀 Logging Mixin Examples")
    print("=" * 50)

    # Example: Basic logged service
    basic_service = LoggedService(ServiceConfig(name="example_logged"))

    # The service now has a contextual logger
    basic_service.logger.info("This is a manually logged message")

    # Method calls are automatically logged
    result = await basic_service.process_data("hello world")
    print(f"Result: {result}")

    print("\n✅ Benefits of the Mixin Approach:")
    print("1. No more separate logged service classes")
    print("2. Mix and match logging with any service type")
    print("3. Automatic method execution logging")
    print("4. Contextual loggers with service metadata")
    print("5. Built-in metrics recording (if available)")

    print("\n🎯 Migration Guide:")
    print("OLD: class MyService(LoggedExtendedService)")
    print("NEW: class MyService(LoggingMixin, ValidatedNATSService)")
    print("\nOLD: class MyAPI(LoggedHTTPService)")
    print("NEW: class MyAPI(HTTPLoggingMixin, HTTPNATSService)")

    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
