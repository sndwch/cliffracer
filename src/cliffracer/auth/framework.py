"""
Role-Based Access Control (RBAC) framework for NATS microservices
"""

import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from functools import wraps
from typing import Any, Optional

import jwt
from nats_service_extended import (
    RPCRequest,
    ServiceConfig,
    ValidatedNATSService,
)

# Global context variable for request context
current_context: ContextVar[Optional["RequestContext"]] = ContextVar(
    "current_context", default=None
)


class Permission(str, Enum):
    """System permissions"""

    READ_USERS = "read:users"
    WRITE_USERS = "write:users"
    DELETE_USERS = "delete:users"
    READ_ORDERS = "read:orders"
    WRITE_ORDERS = "write:orders"
    DELETE_ORDERS = "delete:orders"
    ADMIN_ACCESS = "admin:*"


class Role(str, Enum):
    """System roles"""

    ADMIN = "admin"
    USER = "user"
    MANAGER = "manager"
    GUEST = "guest"


# Role to permissions mapping
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: {Permission.ADMIN_ACCESS},
    Role.MANAGER: {
        Permission.READ_USERS,
        Permission.WRITE_USERS,
        Permission.READ_ORDERS,
        Permission.WRITE_ORDERS,
    },
    Role.USER: {Permission.READ_USERS, Permission.READ_ORDERS, Permission.WRITE_ORDERS},
    Role.GUEST: {Permission.READ_USERS},
}


@dataclass
class RequestContext:
    """Context object carrying authentication and authorization info"""

    user_id: str
    username: str
    roles: list[Role]
    permissions: set[Permission]
    session_id: str
    request_id: str
    created_at: datetime
    expires_at: datetime | None = None
    metadata: dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

        # Compute permissions from roles if not provided
        if not self.permissions:
            self.permissions = set()
            for role in self.roles:
                self.permissions.update(ROLE_PERMISSIONS.get(role, set()))

    def has_permission(self, permission: Permission) -> bool:
        """Check if context has specific permission"""
        return Permission.ADMIN_ACCESS in self.permissions or permission in self.permissions

    def has_role(self, role: Role) -> bool:
        """Check if context has specific role"""
        return role in self.roles

    def has_any_role(self, roles: list[Role]) -> bool:
        """Check if context has any of the specified roles"""
        return any(role in self.roles for role in roles)

    def is_expired(self) -> bool:
        """Check if context is expired"""
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "roles": [role.value for role in self.roles],
            "permissions": [perm.value for perm in self.permissions],
            "session_id": self.session_id,
            "request_id": self.request_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RequestContext":
        """Create from dictionary"""
        return cls(
            user_id=data["user_id"],
            username=data["username"],
            roles=[Role(role) for role in data["roles"]],
            permissions={Permission(perm) for perm in data["permissions"]},
            session_id=data["session_id"],
            request_id=data["request_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data["expires_at"] else None,
            metadata=data.get("metadata", {}),
        )


class AuthenticationError(Exception):
    """Raised when authentication fails"""

    pass


class AuthorizationError(Exception):
    """Raised when authorization fails"""

    pass


class ContextualRPCRequest(RPCRequest):
    """Base RPC request that includes context"""

    _context: RequestContext | None = None

    def set_context(self, context: RequestContext):
        """Set the request context"""
        self._context = context

    def get_context(self) -> RequestContext | None:
        """Get the request context"""
        return self._context


# Approach 1: Explicit Context Parameter
def requires_auth(permissions: list[Permission] = None, roles: list[Role] = None):
    """Decorator for methods that require authentication/authorization"""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract context from kwargs or context var
            context = kwargs.get("context") or current_context.get()

            if not context:
                raise AuthenticationError("No authentication context provided")

            if context.is_expired():
                raise AuthenticationError("Authentication context expired")

            # Check permissions
            if permissions:
                if not any(context.has_permission(perm) for perm in permissions):
                    raise AuthorizationError(f"Missing required permissions: {permissions}")

            # Check roles
            if roles:
                if not context.has_any_role(roles):
                    raise AuthorizationError(f"Missing required roles: {roles}")

            return await func(*args, **kwargs)

        return wrapper

    return decorator


# Approach 2: Context Variable (Automatic Propagation)
class SecureNATSService(ValidatedNATSService):
    """Service base class with authentication support"""

    def __init__(self, config: ServiceConfig, jwt_secret: str = "your-secret-key"):
        super().__init__(config)
        self.jwt_secret = jwt_secret

    async def _handle_rpc_request(self, msg):
        """Enhanced RPC handler with context extraction"""
        try:
            # Parse message data
            data = json.loads(msg.data.decode()) if msg.data else {}

            # Extract context from message
            context_data = data.pop("_context", None)
            context = None

            if context_data:
                try:
                    context = RequestContext.from_dict(context_data)
                    current_context.set(context)
                except Exception as e:
                    error_response = {
                        "error": f"Invalid context: {e}",
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                    await msg.respond(json.dumps(error_response).encode())
                    return

            # Continue with normal RPC handling
            await super()._handle_rpc_request(msg)

        finally:
            # Clear context
            current_context.set(None)

    async def call_rpc_with_context(
        self, service: str, method: str, context: RequestContext = None, **kwargs
    ):
        """RPC call that propagates context"""
        # Use provided context or current context
        ctx = context or current_context.get()

        if ctx:
            kwargs["_context"] = ctx.to_dict()

        return await self.call_rpc(service, method, **kwargs)

    async def call_async_with_context(
        self, service: str, method: str, context: RequestContext = None, **kwargs
    ):
        """Async RPC call that propagates context"""
        ctx = context or current_context.get()

        if ctx:
            kwargs["_context"] = ctx.to_dict()

        await self.call_async(service, method, **kwargs)


# Approach 3: Contextual RPC Decorators
def authenticated_rpc(permissions: list[Permission] = None, roles: list[Role] = None):
    """Decorator that combines RPC with authentication"""

    def decorator(func: Callable) -> Callable:
        # Mark as RPC
        func._is_rpc = True
        func._rpc_name = func.__name__

        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            context = current_context.get()

            if not context:
                raise AuthenticationError("Authentication required")

            if context.is_expired():
                raise AuthenticationError("Authentication expired")

            # Check permissions
            if permissions and not any(context.has_permission(perm) for perm in permissions):
                raise AuthorizationError(f"Missing permissions: {permissions}")

            # Check roles
            if roles and not context.has_any_role(roles):
                raise AuthorizationError(f"Missing roles: {roles}")

            return await func(self, *args, **kwargs)

        return wrapper

    return decorator


# JWT Token Service
class TokenService:
    """Service for creating and validating JWT tokens"""

    def __init__(self, secret_key: str, algorithm: str = "HS256"):
        self.secret_key = secret_key
        self.algorithm = algorithm

    def create_token(self, context: RequestContext, expires_in_hours: int = 24) -> str:
        """Create JWT token from context"""
        payload = context.to_dict()
        payload["exp"] = datetime.utcnow() + timedelta(hours=expires_in_hours)

        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)

    def validate_token(self, token: str) -> RequestContext:
        """Validate JWT token and return context"""
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
            return RequestContext.from_dict(payload)
        except jwt.ExpiredSignatureError as e:
            raise AuthenticationError("Token expired") from e
        except jwt.InvalidTokenError as e:
            raise AuthenticationError("Invalid token") from e


# Helper function to get current context
def get_current_context() -> RequestContext | None:
    """Get the current request context"""
    return current_context.get()


def require_permission(permission: Permission):
    """Decorator to require specific permission"""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            context = get_current_context()
            if not context or not context.has_permission(permission):
                raise AuthorizationError(f"Permission required: {permission}")
            return await func(*args, **kwargs)

        return wrapper

    return decorator


def require_role(role: Role):
    """Decorator to require specific role"""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            context = get_current_context()
            if not context or not context.has_role(role):
                raise AuthorizationError(f"Role required: {role}")
            return await func(*args, **kwargs)

        return wrapper

    return decorator
