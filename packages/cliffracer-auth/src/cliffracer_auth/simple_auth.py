"""JWT token issuance, verification, and handler authorization.

Provides AuthConfig, token signing with HS256, password hashing, and
auth context management for RPC and listener handlers.
"""

import asyncio
import base64
import binascii
import functools
import hashlib
import hmac
import secrets
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

import jwt
from loguru import logger
from pydantic import BaseModel, Field

# Import canonical AuthenticationError and AuthorizationError from core.
from cliffracer.core.decorators import refuse_bare_use
from cliffracer.core.exceptions import AuthenticationError, AuthorizationError

# Context variable for storing auth context
auth_context_var: ContextVar[Optional["AuthContext"]] = ContextVar("auth_context", default=None)


class AuthConfig(BaseModel):
    """Configuration for authentication system"""

    secret_key: str = Field(..., description="Secret key for JWT signing")
    algorithm: str = Field(default="HS256", description="JWT algorithm")
    token_expiry_hours: int = Field(default=24, description="Token expiry in hours")
    enable_auth: bool = Field(default=True, description="Enable authentication")
    pbkdf2_iterations: int = Field(
        default=100_000, description="PBKDF2 iterations for new password hashes"
    )
    refresh_max_lifetime_hours: int | None = Field(
        default=None,
        description=(
            "Cap on total lifetime from the original login, across refreshes. "
            "None means refresh is unbounded, which is deliberate rather than "
            "accidental: a single login then grants access indefinitely."
        ),
    )


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
    """In-memory authentication and JWT issuing service.

    Maintains an in-memory user registry and verifies HMAC SHA-256 tokens.
    State is stored in memory and reset across service restarts.
    """

    def __init__(self, config: AuthConfig):
        self.config = config
        self._users: dict[str, dict] = {}  # In-memory user store
        self._revoked_jtis: set[str] = set()

        if not config.secret_key or len(config.secret_key) < 32:
            raise ValueError("Secret key must be at least 32 characters")

    _HASH_PREFIX = "pbkdf2_sha256"
    _MAX_ITERATIONS = 10_000_000  # ~2s per verify at this ceiling; caps attacker-supplied cost

    @staticmethod
    def _b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _unb64(text: str) -> bytes:
        padding = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(text + padding)

    def hash_password(self, password: str) -> str:
        """Hash a password with a fresh random per-user salt.

        Returns ``pbkdf2_sha256$<iterations>$<salt>$<hash>``, salt and hash
        URL-safe base64 without padding. The encoded form keeps this method's
        one-argument signature while carrying the salt, and carrying the
        iteration count makes it upgradeable later without another break.
        """
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, self.config.pbkdf2_iterations
        )
        return (
            f"{self._HASH_PREFIX}${self.config.pbkdf2_iterations}$"
            f"{self._b64(salt)}${self._b64(digest)}"
        )

    def _legacy_hash(self, password: str) -> str:
        """Deprecated legacy hash verification retained for backward compatibility."""
        salt = self.config.secret_key.encode()[:16]
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000).hex()

    def verify_password(self, password: str, password_hash: str) -> bool:
        """Verify a password against either hash form.

        Malformed input returns False rather than raising: a corrupt stored
        record should fail authentication, not take the service down. This
        covers non-ASCII or non-``str`` stored records and iteration counts
        outside a sane range, not just bad base64/field-count.
        """
        if not isinstance(password_hash, str):
            return False  # type: ignore[unreachable]

        if not password_hash.startswith(f"{self._HASH_PREFIX}$"):
            try:
                legacy = self._legacy_hash(password)
                return hmac.compare_digest(legacy.encode(), password_hash.encode())
            except (ValueError, TypeError, UnicodeEncodeError):
                return False

        try:
            _, iterations_str, salt_b64, digest_b64 = password_hash.split("$")
            iterations = int(iterations_str)
            if not (0 < iterations <= self._MAX_ITERATIONS):
                return False
            salt = self._unb64(salt_b64)
            expected = self._unb64(digest_b64)
            if not salt or not expected:
                return False
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        except (ValueError, TypeError, OverflowError, binascii.Error):
            return False

        return hmac.compare_digest(candidate, expected)

    def create_user(
        self,
        username: str,
        email: str,
        password: str,
        roles: set[str] | None = None,
        permissions: set[str] | None = None,
    ) -> AuthUser:
        """Create a new user"""
        # Absolute since the move: this file is no longer inside cliffracer.
        # A FUNCTION-LOCAL import, which a top-of-file import grep does not
        # see -- it was found by a test, not by reading.
        from cliffracer.core.validation import (
            validate_password,
            validate_string_length,
            validate_username,
        )

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

    def _mint_token(self, user: AuthUser, original_iat: float | None = None) -> str:
        """Sign a JWT for a user. The only place a token is created.

        ``oiat`` is the original issue time, carried unchanged across refreshes
        so ``refresh_max_lifetime_hours`` measures from the original login
        rather than restarting on every refresh. It is framework-internal.
        """
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=self.config.token_expiry_hours)
        issued_at = now.timestamp()
        payload = {
            "jti": secrets.token_hex(16),
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "roles": list(user.roles),
            "permissions": list(user.permissions),
            "exp": expires_at.timestamp(),
            "iat": issued_at,
            "oiat": original_iat if original_iat is not None else issued_at,
        }
        return jwt.encode(payload, self.config.secret_key, algorithm=self.config.algorithm)

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

        if not user_data["password_hash"].startswith(f"{self._HASH_PREFIX}$"):
            user_data["password_hash"] = self.hash_password(password)
            logger.info(f"Upgraded legacy password hash for user {username}")

        token = self._mint_token(user)
        logger.info(f"User {username} authenticated successfully")
        return token

    def validate_token(self, token: str) -> AuthContext | None:
        """Validate JWT token and return auth context"""
        try:
            payload = jwt.decode(token, self.config.secret_key, algorithms=[self.config.algorithm])

            jti = payload.get("jti")
            if jti and jti in self._revoked_jtis:
                logger.warning(f"Token validation failed: token has been revoked (jti={jti})")
                return None

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
        """Issue a new token from a valid one, without a password.

        The old implementation called ``authenticate(username, "")``, and
        ``authenticate`` does not skip the password check, so this returned
        None for every token it was given. Minting now lives in ``_mint_token``
        and there is no password path here at all.
        """
        context = self.validate_token(token)
        if not context or not context.user:
            return None

        # Re-read the user rather than trusting the token's copy: roles,
        # permissions and is_active may all have changed since it was minted,
        # and re-signing a stale snapshot silently extends revoked access.
        user_data = self._users.get(context.user.username)
        if not user_data:
            logger.warning(f"Refresh failed: user {context.user.username} no longer exists")
            return None

        user = user_data["user"]
        if not user.is_active:
            logger.warning(f"Refresh failed: user {user.username} is inactive")
            return None

        try:
            payload = jwt.decode(token, self.config.secret_key, algorithms=[self.config.algorithm])
        except jwt.PyJWTError:
            return None

        # Fall back to 'iat' if 'oiat' is absent.
        original_iat = payload.get("oiat", payload.get("iat"))

        if self.config.refresh_max_lifetime_hours is not None and original_iat is not None:
            deadline = original_iat + self.config.refresh_max_lifetime_hours * 3600
            if datetime.now(UTC).timestamp() > deadline:
                logger.warning(
                    f"Refresh failed: {user.username} exceeded the "
                    f"{self.config.refresh_max_lifetime_hours}h maximum refresh lifetime"
                )
                return None

        logger.info(f"Refreshed token for {user.username}")
        return self._mint_token(user, original_iat=original_iat)

    def revoke_token(self, token: str) -> None:
        """Revoke a JWT token by adding its identifier to the in-memory revoked set."""
        try:
            payload = jwt.decode(
                token,
                self.config.secret_key,
                algorithms=[self.config.algorithm],
                options={"verify_exp": False},
            )
            jti = payload.get("jti")
            if jti:
                self._revoked_jtis.add(jti)
                logger.info(f"Token revoked: jti={jti}")
            else:
                logger.warning("Token revocation failed: missing jti claim in token")
        except jwt.PyJWTError as e:
            logger.warning(f"Token revocation failed: {e}")

    def add_role(self, username: str, role: str) -> None:
        """Add role to user"""
        if username in self._users:
            self._users[username]["user"].roles.add(role)
            logger.info(f"Added role {role} to user {username}")

    def add_permission(self, username: str, permission: str) -> None:
        """Add permission to user"""
        if username in self._users:
            self._users[username]["user"].permissions.add(permission)
            logger.info(f"Added permission {permission} to user {username}")


# Global auth service instance (set by application)
_auth_service: SimpleAuthService | None = None


def get_current_context() -> AuthContext | None:
    """Get current auth context"""
    return auth_context_var.get()


def set_current_context(context: AuthContext) -> None:
    """Set current auth context"""
    auth_context_var.set(context)


def clear_current_context() -> None:
    """Clear current auth context"""
    auth_context_var.set(None)


def get_current_user() -> AuthUser | None:
    """Get current authenticated user"""
    context = get_current_context()
    return context.user if context else None


def requires_auth(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that requires authentication"""

    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        context = get_current_context()
        if not context or not context.is_authenticated:
            raise AuthenticationError("Authentication required")
        return await func(*args, **kwargs)

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        context = get_current_context()
        if not context or not context.is_authenticated:
            raise AuthenticationError("Authentication required")
        return func(*args, **kwargs)

    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper


def requires_roles(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that requires specific roles"""
    # Guard against bare decorator usage without arguments.
    refuse_bare_use(roles[0] if roles else None, "requires_roles", '@requires_roles("admin")')

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            context = get_current_context()
            if not context or not context.is_authenticated:
                raise AuthenticationError("Authentication required")

            user_roles = context.user.roles if context.user else set()
            if not any(role in user_roles for role in roles):
                raise AuthorizationError(f"Required roles: {roles}")

            return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
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


def requires_permissions(*permissions: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that requires specific permissions"""
    # Guard against bare decorator usage without arguments.
    refuse_bare_use(
        permissions[0] if permissions else None,
        "requires_permissions",
        '@requires_permissions("orders:write")',
    )

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            context = get_current_context()
            if not context or not context.is_authenticated:
                raise AuthenticationError("Authentication required")

            user_perms = context.user.permissions if context.user else set()
            if not any(perm in user_perms for perm in permissions):
                raise AuthorizationError(f"Required permissions: {permissions}")

            return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
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


# Middleware for auth integration
class AuthMiddleware:
    """Middleware for extracting auth from requests"""

    def __init__(self, auth_service: SimpleAuthService):
        self.auth_service = auth_service

    async def __call__(self, request: Any, call_next: Callable[..., Any]) -> Any:
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
