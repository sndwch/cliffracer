"""
Examples demonstrating different RBAC patterns in the NATS framework
"""

import asyncio
from datetime import datetime, timedelta

from pydantic import BaseModel

from auth_framework import (
    SecureNATSService,
    AuthenticationError,
    AuthorizationError,
    Permission,
    RequestContext,
    Role,
    TokenService,
    authenticated_rpc,
    get_current_context,
    require_permission,
    require_role,
    requires_auth,
)
from nats_service_extended import RPCRequest, RPCResponse, ServiceConfig, validated_rpc


# Request/Response models
class CreateUserRequest(RPCRequest):
    username: str
    email: str
    role: Role


class UserResponse(RPCResponse):
    user_id: str
    username: str
    email: str
    role: Role


class GetOrdersRequest(RPCRequest):
    user_id: str = None
    limit: int = 10


class OrderResponse(BaseModel):
    order_id: str
    user_id: str
    total: float
    status: str


# Approach 1: Explicit Context Parameter
class UserServiceExplicit(SecureNATSService):
    """User service with explicit context parameters"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.users = {}
        self.user_counter = 0

    @validated_rpc(CreateUserRequest, UserResponse)
    @requires_auth(permissions=[Permission.WRITE_USERS])
    async def create_user(
        self, request: CreateUserRequest, context: RequestContext
    ) -> UserResponse:
        """Create user - requires explicit context parameter"""
        # Context is explicitly passed
        print(f"User {context.username} creating user: {request.username}")

        self.user_counter += 1
        user_id = f"user_{self.user_counter}"

        user = {
            "user_id": user_id,
            "username": request.username,
            "email": request.email,
            "role": request.role,
            "created_by": context.user_id,
        }

        self.users[user_id] = user

        # Propagate context to other services
        await self.call_rpc_with_context(
            "audit_service",
            "log_user_creation",
            context=context,
            user_id=user_id,
            created_by=context.user_id,
        )

        return UserResponse(**user)

    @requires_auth(permissions=[Permission.READ_USERS])
    async def get_user(self, user_id: str, context: RequestContext) -> dict:
        """Get user with explicit context"""
        print(f"User {context.username} fetching user: {user_id}")

        # Check if user can access this data
        user = self.users.get(user_id)
        if not user:
            raise ValueError("User not found")

        # Users can only see their own data unless they have admin role
        if user_id != context.user_id and not context.has_role(Role.ADMIN):
            raise AuthorizationError("Cannot access other user's data")

        return user


# Approach 2: Context Variables (Automatic Propagation)
class UserServiceAuto(SecureNATSService):
    """User service with automatic context propagation"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.users = {}
        self.user_counter = 0

    @authenticated_rpc(permissions=[Permission.WRITE_USERS])
    async def create_user(self, request: CreateUserRequest) -> UserResponse:
        """Create user - context automatically available"""
        # Get context from context variable
        context = get_current_context()
        print(f"User {context.username} creating user: {request.username}")

        self.user_counter += 1
        user_id = f"user_{self.user_counter}"

        user = {
            "user_id": user_id,
            "username": request.username,
            "email": request.email,
            "role": request.role,
            "created_by": context.user_id,
        }

        self.users[user_id] = user

        # Context automatically propagated
        await self.call_rpc_with_context(
            "audit_service", "log_user_creation", user_id=user_id, created_by=context.user_id
        )

        return UserResponse(**user)

    @require_permission(Permission.READ_USERS)
    async def get_user(self, user_id: str) -> dict:
        """Get user with permission decorator"""
        context = get_current_context()
        print(f"User {context.username} fetching user: {user_id}")

        user = self.users.get(user_id)
        if not user:
            raise ValueError("User not found")

        # Check ownership or admin access
        if user_id != context.user_id and not context.has_role(Role.ADMIN):
            raise AuthorizationError("Cannot access other user's data")

        return user

    @require_role(Role.ADMIN)
    async def delete_user(self, user_id: str) -> dict:
        """Delete user - admin only"""
        context = get_current_context()
        print(f"Admin {context.username} deleting user: {user_id}")

        if user_id in self.users:
            user = self.users.pop(user_id)

            # Log admin action
            await self.call_rpc_with_context(
                "audit_service", "log_user_deletion", user_id=user_id, deleted_by=context.user_id
            )

            return {"deleted": True, "user": user}
        else:
            raise ValueError("User not found")


# Order service demonstrating different permission levels
class OrderService(SecureNATSService):
    """Order service with role-based access"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.orders = {}
        self.order_counter = 0

    @authenticated_rpc(permissions=[Permission.WRITE_ORDERS])
    async def create_order(self, items: list[dict], total: float) -> dict:
        """Create order - any authenticated user"""
        context = get_current_context()

        self.order_counter += 1
        order_id = f"order_{self.order_counter}"

        order = {
            "order_id": order_id,
            "user_id": context.user_id,
            "items": items,
            "total": total,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
        }

        self.orders[order_id] = order
        print(f"User {context.username} created order: {order_id}")

        return order

    @authenticated_rpc(permissions=[Permission.READ_ORDERS])
    async def get_orders(self, user_id: str = None, limit: int = 10) -> list[dict]:
        """Get orders with role-based filtering"""
        context = get_current_context()

        # Determine which orders user can see
        if context.has_role(Role.ADMIN) or context.has_role(Role.MANAGER):
            # Admins and managers can see all orders
            target_user_id = user_id or None
        else:
            # Regular users can only see their own orders
            target_user_id = context.user_id

        orders = []
        for order in self.orders.values():
            if target_user_id is None or order["user_id"] == target_user_id:
                orders.append(order)
                if len(orders) >= limit:
                    break

        print(f"User {context.username} fetched {len(orders)} orders")
        return orders

    @require_role(Role.ADMIN)
    async def delete_order(self, order_id: str) -> dict:
        """Delete order - admin only"""
        context = get_current_context()

        if order_id in self.orders:
            order = self.orders.pop(order_id)
            print(f"Admin {context.username} deleted order: {order_id}")
            return {"deleted": True, "order": order}
        else:
            raise ValueError("Order not found")


# Audit service for logging actions
class AuditService(SecureNATSService):
    """Audit service that logs all actions"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.audit_logs = []

    async def log_user_creation(self, user_id: str, created_by: str):
        """Log user creation"""
        context = get_current_context()

        log_entry = {
            "action": "user_created",
            "user_id": user_id,
            "created_by": created_by,
            "request_id": context.request_id if context else None,
            "timestamp": datetime.utcnow().isoformat(),
        }

        self.audit_logs.append(log_entry)
        print(f"Audit: User {user_id} created by {created_by}")

    async def log_user_deletion(self, user_id: str, deleted_by: str):
        """Log user deletion"""
        context = get_current_context()

        log_entry = {
            "action": "user_deleted",
            "user_id": user_id,
            "deleted_by": deleted_by,
            "request_id": context.request_id if context else None,
            "timestamp": datetime.utcnow().isoformat(),
        }

        self.audit_logs.append(log_entry)
        print(f"Audit: User {user_id} deleted by {deleted_by}")

    @require_role(Role.ADMIN)
    async def get_audit_logs(self, limit: int = 100) -> list[dict]:
        """Get audit logs - admin only"""
        context = get_current_context()
        print(f"Admin {context.username} fetching audit logs")

        return self.audit_logs[-limit:]


# Authentication service
class AuthService(SecureNATSService):
    """Authentication service"""

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.token_service = TokenService("your-secret-key")
        self.sessions = {}

        # Mock user database
        self.user_db = {
            "admin": {"password": "admin123", "role": Role.ADMIN},
            "manager": {"password": "manager123", "role": Role.MANAGER},
            "user": {"password": "user123", "role": Role.USER},
            "guest": {"password": "guest123", "role": Role.GUEST},
        }

    async def login(self, username: str, password: str) -> dict:
        """Authenticate user and return token"""
        user = self.user_db.get(username)

        if not user or user["password"] != password:
            raise AuthenticationError("Invalid credentials")

        # Create context
        context = RequestContext(
            user_id=f"user_{username}",
            username=username,
            roles=[user["role"]],
            permissions=set(),  # Will be computed automatically
            session_id=f"session_{len(self.sessions)}",
            request_id=f"req_{datetime.utcnow().timestamp()}",
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=24),
        )

        # Generate token
        token = self.token_service.create_token(context)

        # Store session
        self.sessions[context.session_id] = context

        print(f"User {username} logged in with role {user['role']}")

        return {
            "token": token,
            "user_id": context.user_id,
            "username": username,
            "role": user["role"].value,
            "expires_at": context.expires_at.isoformat(),
        }

    async def validate_token(self, token: str) -> RequestContext:
        """Validate token and return context"""
        return self.token_service.validate_token(token)


async def demonstrate_auth_patterns():
    """Demonstrate different authentication patterns"""
    print("=== RBAC Authentication Patterns Demo ===\n")

    # Create auth service
    auth_config = ServiceConfig(name="auth_service")
    auth_service = AuthService(auth_config)

    try:
        print("1. AUTHENTICATION")
        print("-" * 40)

        # Login as different users
        admin_auth = await auth_service.login("admin", "admin123")
        manager_auth = await auth_service.login("manager", "manager123")
        user_auth = await auth_service.login("user", "user123")

        print(f"Admin token: {admin_auth['token'][:50]}...")
        print(f"Manager token: {manager_auth['token'][:50]}...")
        print(f"User token: {user_auth['token'][:50]}...")

        # Validate tokens and create contexts
        admin_context = await auth_service.validate_token(admin_auth["token"])
        manager_context = await auth_service.validate_token(manager_auth["token"])
        user_context = await auth_service.validate_token(user_auth["token"])

        print(f"\nAdmin permissions: {[p.value for p in admin_context.permissions]}")
        print(f"Manager permissions: {[p.value for p in manager_context.permissions]}")
        print(f"User permissions: {[p.value for p in user_context.permissions]}")

        print("\n2. ROLE-BASED ACCESS CONTROL")
        print("-" * 40)

        # Create services
        user_service = UserServiceAuto(ServiceConfig(name="user_service"))
        order_service = OrderService(ServiceConfig(name="order_service"))
        audit_service = AuditService(ServiceConfig(name="audit_service"))

        # Mock the RPC calls to work in demo
        user_service.call_rpc_with_context = lambda *args, **kwargs: asyncio.sleep(0.1)
        order_service.call_rpc_with_context = lambda *args, **kwargs: asyncio.sleep(0.1)

        # Test admin operations
        print("\nAdmin Operations:")
        from contextvars import copy_context

        # Set admin context
        ctx = copy_context()
        ctx.run(lambda: current_context.set(admin_context))

        async def admin_operations():
            current_context.set(admin_context)

            # Admin can create users
            create_req = CreateUserRequest(
                username="newuser", email="new@example.com", role=Role.USER
            )
            new_user = await user_service.create_user(create_req)
            print(f"✓ Admin created user: {new_user.username}")

            # Admin can delete users
            deleted = await user_service.delete_user(new_user.user_id)
            print(f"✓ Admin deleted user: {deleted['user']['username']}")

            # Admin can view audit logs
            logs = await audit_service.get_audit_logs(limit=5)
            print(f"✓ Admin viewed {len(logs)} audit logs")

        await admin_operations()

        # Test manager operations
        print("\nManager Operations:")

        async def manager_operations():
            current_context.set(manager_context)

            # Manager can create users
            create_req = CreateUserRequest(
                username="employee", email="employee@example.com", role=Role.USER
            )
            new_user = await user_service.create_user(create_req)
            print(f"✓ Manager created user: {new_user.username}")

            # Manager can view all orders
            orders = await order_service.get_orders(limit=10)
            print(f"✓ Manager viewed {len(orders)} orders")

            # Manager cannot delete users (will fail)
            try:
                await user_service.delete_user(new_user.user_id)
            except AuthorizationError as e:
                print(f"✗ Manager cannot delete users: {e}")

        await manager_operations()

        # Test regular user operations
        print("\nUser Operations:")

        async def user_operations():
            current_context.set(user_context)

            # User can create orders
            order = await order_service.create_order(
                items=[{"product": "widget", "qty": 2}], total=29.99
            )
            print(f"✓ User created order: {order['order_id']}")

            # User can view their own orders
            orders = await order_service.get_orders(user_id=user_context.user_id)
            print(f"✓ User viewed {len(orders)} of their orders")

            # User cannot create users (will fail)
            try:
                create_req = CreateUserRequest(
                    username="hacker", email="hacker@example.com", role=Role.ADMIN
                )
                await user_service.create_user(create_req)
            except AuthorizationError as e:
                print(f"✗ User cannot create users: {e}")

        await user_operations()

        print("\n3. PERMISSION VALIDATION")
        print("-" * 40)

        # Test permission checks
        print(f"Admin has admin access: {admin_context.has_permission(Permission.ADMIN_ACCESS)}")
        print(f"Manager has write users: {manager_context.has_permission(Permission.WRITE_USERS)}")
        print(f"User has write users: {user_context.has_permission(Permission.WRITE_USERS)}")
        print(f"User has read orders: {user_context.has_permission(Permission.READ_ORDERS)}")

    except Exception as e:
        print(f"Error in demo: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    from auth_framework import current_context

    asyncio.run(demonstrate_auth_patterns())
