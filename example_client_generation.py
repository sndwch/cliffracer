"""
Example demonstrating simple client generation
"""

import asyncio

from pydantic import BaseModel

from cliffracer import NATSService, ServiceConfig, rpc


class UserRequest(BaseModel):
    username: str
    email: str


class UserResponse(BaseModel):
    user_id: str
    username: str
    status: str


class ExampleService(NATSService):
    """Example service for testing client generation"""

    def __init__(self):
        config = ServiceConfig(name="example_service", version="1.0.0")
        super().__init__(config)

    @rpc
    async def create_user(self, username: str, email: str) -> dict:
        """Create a new user"""
        return {"user_id": f"user_{username}", "username": username, "status": "created"}

    @rpc
    async def get_user(self, user_id: str) -> dict:
        """Get user by ID"""
        return {
            "user_id": user_id,
            "username": f"user_for_{user_id}",
            "email": f"{user_id}@example.com",
            "status": "active",
        }

    @rpc
    async def list_users(self, limit: int = 10) -> dict:
        """List users with pagination"""
        return {
            "users": [
                {"user_id": f"user_{i}", "username": f"user{i}"} for i in range(1, limit + 1)
            ],
            "total": limit,
        }


async def main():
    """Run the example service"""
    import nats

    print("🚀 Starting example service for client generation testing...")

    # Start NATS connection
    nc = await nats.connect("nats://localhost:4222")

    # Create and start service
    service = ExampleService()
    service.nc = nc

    # Register RPC handlers manually for this example
    await service.start()

    print("✅ Example service running!")
    print("   Service name: example_service")
    print("   Available methods: create_user, get_user, list_users")
    print()
    print("🔧 To generate a client, run:")
    print("   python scripts/generate_client.py example_service example_client.py")
    print()
    print("Press Ctrl+C to stop...")

    try:
        # Keep service running
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Stopping service...")
        await service.stop()
        await nc.close()
        print("✅ Service stopped")


if __name__ == "__main__":
    asyncio.run(main())
