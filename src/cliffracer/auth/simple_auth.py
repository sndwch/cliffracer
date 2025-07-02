"""
Simple authentication system for Cliffracer services

This provides a basic but functional authentication system using JWT tokens.
It's designed to be simple, secure, and easy to integrate with existing services.
"""

import asyncio
import functools
import hashlib
import hmac
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Optional

import jwt
from loguru import logger
from pydantic import BaseModel, Field

# Context variable for storing auth context
auth_context_var: ContextVar[Optional["AuthContext"]] = ContextVar("auth_context", default=None)


class AuthConfig(BaseModel):
    """Configuration for authentication system"""

    secret_key: str = Field(..., description="Secret key for JWT signing")
    algorithm: str = Field(default="HS256", description="JWT algorithm")
    token_expiry_hours: int = Field(default=24, description="Token expiry in hours")
    enable_auth: bool = Field(default=True, description="Enable authentication")
    bcrypt_rounds: int = Field(default=12, description="Bcrypt hashing rounds")


@dataclass
class AuthUser:
    """Authenticated user information"""

    user_id: str
    username: str
    email: str
    roles: set[str] = field(default_factory=set)
    permissions: set[str] = field(default_factory=set)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    is_active: bool = True


@dataclass
class AuthContext:
    """Current authentication context"""

    user: AuthUser | None = None
    token: str | None = None
    expires_at: datetime | None = None

    @property
    def is_authenticated(self) -> bool:
        """Check if context is authenticated"""
        return self.user is not None and self.is_valid

    @property
    def is_valid(self) -> bool:
        """Check if auth is still valid"""
        if not self.expires_at:
            return False
        return datetime.now(UTC) < self.expires_at


class SimpleAuthService:
    """
    Simple authentication service with JWT tokens.

    This provides basic authentication without external dependencies.
    For production use, consider integrating with OAuth2/OIDC providers.
    """

    def __init__(self, config: AuthConfig):
        self.config = config
        self._users: dict[str, dict] = {}  # In-memory user store
        self._refresh_tokens: dict[str, str] = {}  # Refresh token mapping

        if not config.secret_key or len(config.secret_key) < 32:
            raise ValueError("Secret key must be at least 32 characters")

    def hash_password(self, password: str) -> str:
        """Hash password using PBKDF2"""
        # Simple but secure password hashing
        salt = self.config.secret_key.encode()[:16]  # Use part of secret as salt
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000).hex()

    def verify_password(self, password: str, password_hash: str) -> bool:
        """Verify password against hash"""
        return hmac.compare_digest(self.hash_password(password), password_hash)

    def create_user(
        self,
        username: str,
        email: str,
        password: str,
        roles: set[str] | None = None,
        permissions: set[str] | None = None,
    ) -> AuthUser:
        """Create a new user"""
        from ..core.validation import validate_password, validate_string_length, validate_username

        # Validate inputs
        username = validate_username(username)
        password = validate_password(password)
        email = validate_string_length(email, min_length=3, max_length=254, field_name="Email")

        # Basic email validation
        if "@" not in email or "." not in email.split("@")[1]:
            raise ValueError("Invalid email format")

        if username in self._users:
            raise ValueError(f"User {username} already exists")

        user_id = f"user_{len(self._users) + 1}"
        user = AuthUser(
            user_id=user_id,
            username=username,
            email=email,
            roles=roles or set(),
            permissions=permissions or set(),
        )

        self._users[username] = {"user": user, "password_hash": self.hash_password(password)}

        logger.info(f"Created user: {username}")
        return user

    def authenticate(self, username: str, password: str) -> str | None:
        """Authenticate user and return JWT token"""
        user_data = self._users.get(username)
        if not user_data:
            logger.warning(f"Authentication failed: user {username} not found")
            return None

        if not self.verify_password(password, user_data["password_hash"]):
            logger.warning(f"Authentication failed: invalid password for {username}")
            return None

        user = user_data["user"]
        if not user.is_active:
            logger.warning(f"Authentication failed: user {username} is inactive")
            return None

        # Create JWT token
        expires_at = datetime.now(UTC) + timedelta(hours=self.config.token_expiry_hours)
        payload = {
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "roles": list(user.roles),
            "permissions": list(user.permissions),
            "exp": expires_at.timestamp(),
            "iat": datetime.now(UTC).timestamp(),
        }

        token = jwt.encode(payload, self.config.secret_key, algorithm=self.config.algorithm)
        logger.info(f"User {username} authenticated successfully")
        return token

    def validate_token(self, token: str) -> AuthContext | None:
        """Validate JWT token and return auth context"""
        try:
            payload = jwt.decode(token, self.config.secret_key, algorithms=[self.config.algorithm])

            # Reconstruct user from payload
            user = AuthUser(
                user_id=payload["user_id"],
                username=payload["username"],
                email=payload["email"],
                roles=set(payload.get("roles", [])),
                permissions=set(payload.get("permissions", [])),
            )

            # Create auth context
            context = AuthContext(
                user=user, token=token, expires_at=datetime.fromtimestamp(payload["exp"], UTC)
            )

            return context

        except jwt.ExpiredSignatureError:
            logger.warning("Token validation failed: expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"Token validation failed: {e}")
            return None

    def refresh_token(self, token: str) -> str | None:
        """Refresh an existing token"""
        context = self.validate_token(token)
        if not context or not context.user:
            return None

        # Issue new token with fresh expiry
        return self.authenticate(context.user.username, "")  # Skip password check for refresh

    def revoke_token(self, token: str):
        """Revoke a token (would need persistent storage in production)"""
        # In production, store revoked tokens in Redis/DB until expiry
        logger.info("Token revoked (not persisted in simple implementation)")

    def add_role(self, username: str, role: str):
        """Add role to user"""
        if username in self._users:
            self._users[username]["user"].roles.add(role)
            logger.info(f"Added role {role} to user {username}")

    def add_permission(self, username: str, permission: str):
        """Add permission to user"""
        if username in self._users:
            self._users[username]["user"].permissions.add(permission)
            logger.info(f"Added permission {permission} to user {username}")


# Global auth service instance (set by application)
_auth_service: SimpleAuthService | None = None


def set_auth_service(service: SimpleAuthService):
    """Set the global auth service"""
    global _auth_service
    _auth_service = service


def get_auth_service() -> SimpleAuthService | None:
    """Get the global auth service"""
    return _auth_service


def get_current_context() -> AuthContext | None:
    """Get current auth context"""
    return auth_context_var.get()


def set_current_context(context: AuthContext):
    """Set current auth context"""
    auth_context_var.set(context)


def clear_current_context():
    """Clear current auth context"""
    auth_context_var.set(None)


def get_current_user() -> AuthUser | None:
    """Get current authenticated user"""
    context = get_current_context()
    return context.user if context else None


def requires_auth(func: Callable) -> Callable:
    """Decorator that requires authentication"""

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        context = get_current_context()
        if not context or not context.is_authenticated:
            raise AuthenticationError("Authentication required")
        return await func(*args, **kwargs)

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        context = get_current_context()
        if not context or not context.is_authenticated:
            raise AuthenticationError("Authentication required")
        return func(*args, **kwargs)

    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper


def requires_roles(*roles: str) -> Callable:
    """Decorator that requires specific roles"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            context = get_current_context()
            if not context or not context.is_authenticated:
                raise AuthenticationError("Authentication required")

            user_roles = context.user.roles if context.user else set()
            if not any(role in user_roles for role in roles):
                raise AuthorizationError(f"Required roles: {roles}")

            return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            context = get_current_context()
            if not context or not context.is_authenticated:
                raise AuthenticationError("Authentication required")

            user_roles = context.user.roles if context.user else set()
            if not any(role in user_roles for role in roles):
                raise AuthorizationError(f"Required roles: {roles}")

            return func(*args, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def requires_permissions(*permissions: str) -> Callable:
    """Decorator that requires specific permissions"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            context = get_current_context()
            if not context or not context.is_authenticated:
                raise AuthenticationError("Authentication required")

            user_perms = context.user.permissions if context.user else set()
            if not any(perm in user_perms for perm in permissions):
                raise AuthorizationError(f"Required permissions: {permissions}")

            return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            context = get_current_context()
            if not context or not context.is_authenticated:
                raise AuthenticationError("Authentication required")

            user_perms = context.user.permissions if context.user else set()
            if not any(perm in user_perms for perm in permissions):
                raise AuthorizationError(f"Required permissions: {permissions}")

            return func(*args, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


class AuthenticationError(Exception):
    """Raised when authentication fails"""

    pass


class AuthorizationError(Exception):
    """Raised when authorization fails"""

    pass


# Middleware for auth integration
class AuthMiddleware:
    """Middleware for extracting auth from requests"""

    def __init__(self, auth_service: SimpleAuthService):
        self.auth_service = auth_service

    async def __call__(self, request, call_next):
        """Extract auth token from request headers"""
        auth_header = request.headers.get("Authorization", "")

        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            context = self.auth_service.validate_token(token)
            if context:
                set_current_context(context)

        try:
            response = await call_next(request)
            return response
        finally:
            clear_current_context()
