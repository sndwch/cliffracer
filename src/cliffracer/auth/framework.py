"""
Authentication framework for Cliffracer services

This module provides compatibility interfaces that delegate to the working SimpleAuthService.
For new development, use SimpleAuthService directly from cliffracer.auth.simple_auth.
"""

import warnings
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Permission:
    """A permission that can be granted to users or roles"""

    name: str
    description: str = ""
    resource: str | None = None
    action: str | None = None


@dataclass
class Role:
    """A role that groups permissions"""

    name: str
    description: str = ""
    permissions: list[Permission] = field(default_factory=list)


@dataclass
class User:
    """A user in the authentication system"""

    user_id: str
    username: str
    email: str
    roles: list[Role] = field(default_factory=list)
    permissions: list[Permission] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_login: datetime | None = None


@dataclass
class AuthToken:
    """An authentication token"""

    token: str
    user_id: str
    expires_at: datetime
    scopes: list[str] = field(default_factory=list)


@dataclass
class RequestContext:
    """Context information for an authenticated request"""

    user: User | None = None
    token: AuthToken | None = None
    permissions: list[Permission] = field(default_factory=list)
    roles: list[Role] = field(default_factory=list)


class AuthenticationError(Exception):
    """Raised when authentication fails"""

    pass


class AuthorizationError(Exception):
    """Raised when authorization fails"""

    pass


class TokenService:
    """Service for managing authentication tokens"""

    def __init__(self, secret_key: str) -> None:
        self.secret_key = secret_key
        self.users: dict[str, User] = {}
        self.tokens: dict[str, AuthToken] = {}

    def create_user(self, username: str, email: str, password: str) -> User:
        """Create a new user"""
        from .simple_auth import get_auth_service

        auth_service = get_auth_service()
        if not auth_service:
            raise RuntimeError("SimpleAuthService not initialized. Call set_auth_service() first.")

        # Create user in simple auth service
        auth_user = auth_service.create_user(username, email, password)

        # Convert to framework User
        return User(
            user_id=auth_user.user_id,
            username=auth_user.username,
            email=auth_user.email,
            roles=[],  # Could map roles if needed
            permissions=[],  # Could map permissions if needed
        )

    def authenticate(self, username: str, password: str) -> AuthToken | None:
        """Authenticate a user and return a token"""
        from .simple_auth import get_auth_service

        auth_service = get_auth_service()
        if not auth_service:
            raise RuntimeError("SimpleAuthService not initialized. Call set_auth_service() first.")

        # Authenticate with simple auth service
        token = auth_service.authenticate(username, password)
        if not token:
            return None

        # Validate token to get expiry info
        context = auth_service.validate_token(token)
        if not context:
            return None

        return AuthToken(
            token=token,
            user_id=context.user.user_id,
            expires_at=context.expires_at,
            scopes=[],  # Could add scopes if needed
        )

    def validate_token(self, token: str) -> AuthToken | None:
        """Validate a token and return token info"""
        from .simple_auth import get_auth_service

        auth_service = get_auth_service()
        if not auth_service:
            raise RuntimeError("SimpleAuthService not initialized. Call set_auth_service() first.")

        # Validate with simple auth service
        context = auth_service.validate_token(token)
        if not context or not context.user:
            return None

        return AuthToken(
            token=token, user_id=context.user.user_id, expires_at=context.expires_at, scopes=[]
        )

    def revoke_token(self, token: str) -> None:
        """Revoke a token"""
        from .simple_auth import get_auth_service

        auth_service = get_auth_service()
        if not auth_service:
            raise RuntimeError("SimpleAuthService not initialized. Call set_auth_service() first.")

        # Revoke with simple auth service
        auth_service.revoke_token(token)


# Context variable (placeholder)
current_context: RequestContext | None = None


def requires_auth(*permissions: Permission, roles: list[Role] | None = None) -> Any:
    """Decorator to require authentication and authorization"""

    def decorator(func: Any) -> Any:
        warnings.warn(
            "requires_auth decorator is not functional. Auth system is broken.",
            UserWarning,
            stacklevel=2,
        )
        return func

    return decorator


def authenticated_rpc(func: Any) -> Any:
    """Decorator to require authentication for RPC methods"""
    warnings.warn(
        "authenticated_rpc decorator is not functional. Auth system is broken.",
        UserWarning,
        stacklevel=2,
    )
    return func


def has_permission(permission: Permission, context: RequestContext | None = None) -> bool:
    """Check if the current context has a specific permission"""
    warnings.warn(
        "has_permission is not functional. Auth system is broken.", UserWarning, stacklevel=2
    )
    return False


def has_role(role: Role, context: RequestContext | None = None) -> bool:
    """Check if the current context has a specific role"""
    warnings.warn("has_role is not functional. Auth system is broken.", UserWarning, stacklevel=2)
    return False


def require_auth(*permissions: list[Permission] | None, roles: list[Role] | None = None) -> Any:
    """Decorator that requires specific permissions or roles"""

    def decorator(func: Any) -> Any:
        warnings.warn(
            "require_auth decorator is not functional. Auth system is broken.",
            UserWarning,
            stacklevel=2,
        )

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return wrapper

    return decorator


# Placeholder class for backward compatibility
class AuthenticatedService:
    """Placeholder for authenticated service class"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            "AuthenticatedService is not functional. Auth system is broken. "
            "Use regular NATSService with external auth instead.",
            UserWarning,
            stacklevel=2,
        )


def get_current_user() -> User | None:
    """Get the current authenticated user"""
    warnings.warn(
        "get_current_user is not functional. Auth system is broken.", UserWarning, stacklevel=2
    )
    return None


def set_current_context(context: RequestContext) -> None:
    """Set the current request context"""
    warnings.warn(
        "set_current_context is not functional. Auth system is broken.", UserWarning, stacklevel=2
    )


def clear_current_context() -> None:
    """Clear the current request context"""
    warnings.warn(
        "clear_current_context is not functional. Auth system is broken.", UserWarning, stacklevel=2
    )
