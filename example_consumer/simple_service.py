#!/usr/bin/env python3
"""Simple example service using Cliffracer"""

from cliffracer import CliffracerService, ServiceConfig

class ExampleService(CliffracerService):
    def __init__(self):
        config = ServiceConfig(
            name="example_service",
            nats_url="nats://localhost:4222"
        )
        super().__init__(config)

    @self.rpc
    async def hello(self, name: str = "World") -> str:
        return f"Hello, {name}!"

    @self.rpc  
    async def add(self, a: int, b: int) -> int:
        return a + b

if __name__ == "__main__":
    service = ExampleService()
    print("🚀 Starting example service...")
    print("💡 Try: await call_rpc('example_service', 'hello', name='Alice')")
    service.run()
