"""
Example User Service with Database Integration
=============================================

This example demonstrates a user service that uses PostgreSQL for persistence
instead of in-memory storage.
"""

import asyncio

from cliffracer import (
    Message,
    RPCRequest,
    RPCResponse,
    ServiceConfig,
    ValidatedNATSService,
    broadcast,
    listener,
    validated_rpc,
)
from cliffracer.database import DatabaseConnection, Repository
from cliffracer.database.models import User


# Request/Response models
class CreateUserRequest(RPCRequest):
    """Request to create a new user"""
    user_id: str
    email: str
    name: str


class CreateUserResponse(RPCResponse):
    """Response from user creation"""
    user: dict


class GetUserRequest(RPCRequest):
    """Request to get user by ID"""
    user_id: str


class GetUserResponse(RPCResponse):
    """Response with user data"""
    user: dict | None


class UpdateUserRequest(RPCRequest):
    """Request to update user"""
    user_id: str
    name: str | None = None
    email: str | None = None
    status: str | None = None


class UpdateUserResponse(RPCResponse):
    """Response from user update"""
    user: dict


# Broadcast messages
class UserCreatedMessage(Message):
    """Broadcast when a user is created"""
    user_id: str
    email: str
    name: str


class UserUpdatedMessage(Message):
    """Broadcast when a user is updated"""
    user_id: str
    changes: dict


class UserServiceWithDB(ValidatedNATSService):
    """
    User service with database persistence.

    This service demonstrates:
    - Database connection management
    - Repository pattern usage
    - Transaction support
    - Proper error handling
    - Event broadcasting
    """

    def __init__(self):
        config = ServiceConfig(
            name="user_service_db",
            description="User service with PostgreSQL storage"
        )
        super().__init__(config)

        # Initialize database connection and repository
        self.db = DatabaseConnection()
        self.user_repo = Repository(User, self.db)

    async def on_startup(self):
        """Initialize database connection on startup"""
        await super().on_startup()
        await self.db.connect()
        self.logger.info("Database connection established")

    async def on_shutdown(self):
        """Clean up database connection on shutdown"""
        await self.db.disconnect()
        await super().on_shutdown()

    @validated_rpc(CreateUserRequest, CreateUserResponse)
    async def create_user(self, request: CreateUserRequest) -> CreateUserResponse:
        """
        Create a new user in the database.

        Validates uniqueness and broadcasts creation event.
        """
        try:
            # Check if user already exists
            existing = await self.user_repo.find_one(user_id=request.user_id)
            if existing:
                return CreateUserResponse(
                    success=False,
                    error=f"User {request.user_id} already exists"
                )

            # Check email uniqueness
            email_exists = await self.user_repo.exists(email=request.email)
            if email_exists:
                return CreateUserResponse(
                    success=False,
                    error=f"Email {request.email} is already registered"
                )

            # Create user
            user = User(
                user_id=request.user_id,
                email=request.email,
                name=request.name,
                status="active"
            )

            created_user = await self.user_repo.create(user)

            # Broadcast user created event
            await self.announce_user_created(
                created_user.user_id,
                created_user.email,
                created_user.name
            )

            self.logger.info(f"Created user {created_user.user_id}")

            return CreateUserResponse(
                success=True,
                user=created_user.model_dump()
            )

        except Exception as e:
            self.logger.error(f"Error creating user: {e}")
            return CreateUserResponse(
                success=False,
                error=str(e)
            )

    @validated_rpc(GetUserRequest, GetUserResponse)
    async def get_user(self, request: GetUserRequest) -> GetUserResponse:
        """Get user by ID from database"""
        try:
            user = await self.user_repo.find_one(user_id=request.user_id)

            if user:
                return GetUserResponse(
                    success=True,
                    user=user.model_dump()
                )
            else:
                return GetUserResponse(
                    success=True,
                    user=None
                )

        except Exception as e:
            self.logger.error(f"Error getting user: {e}")
            return GetUserResponse(
                success=False,
                error=str(e),
                user=None
            )

    @validated_rpc(UpdateUserRequest, UpdateUserResponse)
    async def update_user(self, request: UpdateUserRequest) -> UpdateUserResponse:
        """Update user in database"""
        try:
            # Find user
            user = await self.user_repo.find_one(user_id=request.user_id)
            if not user:
                return UpdateUserResponse(
                    success=False,
                    error=f"User {request.user_id} not found"
                )

            # Prepare updates
            updates = {}
            if request.name is not None:
                updates["name"] = request.name
            if request.email is not None:
                # Check email uniqueness
                email_user = await self.user_repo.find_one(email=request.email)
                if email_user and email_user.id != user.id:
                    return UpdateUserResponse(
                        success=False,
                        error=f"Email {request.email} is already in use"
                    )
                updates["email"] = request.email
            if request.status is not None:
                updates["status"] = request.status

            if not updates:
                return UpdateUserResponse(
                    success=True,
                    user=user.model_dump()
                )

            # Update user
            updated_user = await self.user_repo.update(user.id, **updates)

            # Broadcast update event
            await self.announce_user_updated(user.user_id, updates)

            self.logger.info(f"Updated user {user.user_id}: {updates}")

            return UpdateUserResponse(
                success=True,
                user=updated_user.model_dump()
            )

        except Exception as e:
            self.logger.error(f"Error updating user: {e}")
            return UpdateUserResponse(
                success=False,
                error=str(e)
            )

    @broadcast(UserCreatedMessage)
    async def announce_user_created(self, user_id: str, email: str, name: str):
        """Broadcast user creation event"""
        return UserCreatedMessage(
            user_id=user_id,
            email=email,
            name=name
        )

    @broadcast(UserUpdatedMessage)
    async def announce_user_updated(self, user_id: str, changes: dict):
        """Broadcast user update event"""
        return UserUpdatedMessage(
            user_id=user_id,
            changes=changes
        )

    @listener(UserCreatedMessage)
    async def on_user_created_elsewhere(self, message: UserCreatedMessage):
        """
        Handle user creation from other services.

        This could be used to maintain a local cache or trigger
        additional processing.
        """
        self.logger.info(
            f"User created in another service: {message.user_id}"
        )

    # Additional database operations

    async def list_users(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """List users with pagination"""
        users = await self.user_repo.list(limit=limit, offset=offset)
        return [user.model_dump() for user in users]

    async def search_users(self, email: str | None = None, status: str | None = None) -> list[dict]:
        """Search users by criteria"""
        criteria = {}
        if email:
            criteria["email"] = email
        if status:
            criteria["status"] = status

        users = await self.user_repo.find_by(**criteria)
        return [user.model_dump() for user in users]

    async def delete_user(self, user_id: str) -> bool:
        """Soft delete a user (set status to deleted)"""
        user = await self.user_repo.find_one(user_id=user_id)
        if not user:
            return False

        await self.user_repo.update(user.id, status="deleted")
        self.logger.info(f"Soft deleted user {user_id}")
        return True

    async def get_user_count(self, status: str | None = None) -> int:
        """Get count of users, optionally filtered by status"""
        if status:
            return await self.user_repo.count(status=status)
        return await self.user_repo.count()


async def main():
    """Run the database-enabled user service"""
    print("🚀 Starting User Service with Database")
    print("=" * 50)
    print("Make sure PostgreSQL is running!")
    print("Database: postgresql://cliffracer:cliffracer123@localhost:5432/cliffracer")
    print("=" * 50)

    service = UserServiceWithDB()
    await service.start()

    print("\nService is running. Available RPC methods:")
    print("- create_user")
    print("- get_user")
    print("- update_user")

    # Keep service running
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\n🛑 Shutting down user service...")
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
