"""
The async backdoor, declared as an extension.

This example starts a simple service with the debug console enabled.
Requires the `cliffracer-backdoor` distribution; `uv sync --all-packages`
installs it from this workspace.

Connect with: cliffracer-backdoor 127.0.0.1:12345
Password: test123

Try these commands in the backdoor:
  service                  - Inspect service instance
  inspect_service()        - Show service info
  inspect_nats()           - Show NATS connection
  show_handlers()          - List all handlers
  await service.test()     - Call RPC method directly
"""

import asyncio

from cliffracer_backdoor import BackdoorExtension

from cliffracer import CliffracerService, ServiceConfig, rpc


class TestService(CliffracerService):
    """Simple test service with the debug console declared as an extension."""

    # The console is OFF unless asked for: it evaluates arbitrary Python in the
    # service process, so enabling it is always explicit.
    backdoor = BackdoorExtension(enabled=True, port=12345, password="test123")

    def __init__(self):
        super().__init__(ServiceConfig(name="test_service"))

    @rpc
    async def test(self, message: str = "hello") -> dict[str, str]:
        """Test RPC method"""
        return {"message": f"You said: {message}", "status": "ok"}

    @rpc
    async def add(self, a: int, b: int) -> dict[str, int]:
        """Add two numbers"""
        return {"result": a + b}


async def main():
    """Run the test service"""
    print("=" * 70)
    print("Async Backdoor Test Service")
    print("=" * 70)
    print("\nStarting service with backdoor enabled...")
    print("Password: test123")
    print("\nTo connect:")
    print("  cliffracer-backdoor 127.0.0.1:12345")
    print("\nOnce connected, try:")
    print("  service                    - The service instance")
    print("  inspect_service()          - Service details")
    print("  inspect_nats()             - NATS connection info")
    print("  show_handlers()            - List handlers")
    print("  await service.test()       - Call RPC method")
    print("  await nc.publish('test', b'hi')  - Publish NATS message")
    print("\nPress Ctrl+C to stop")
    print("=" * 70)

    service = TestService()
    await service.start()

    try:
        # Keep running
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n\nStopping service...")
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
