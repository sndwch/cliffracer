"""JWT authentication for cliffracer services.

`AuthExtension` puts verification on the per-message hook chain, so a handler
that runs has been authenticated. The decorators and `SimpleAuthService` provide
declarative authentication and role enforcement.
"""

from cliffracer_auth.extension import AuthExtension
from cliffracer_auth.simple_auth import (
    AuthConfig,
    AuthContext,
    AuthenticationError,
    AuthMiddleware,
    AuthorizationError,
    AuthUser,
    SimpleAuthService,
    get_current_context,
    get_current_user,
    requires_auth,
    requires_permissions,
    requires_roles,
)

__all__ = [
    "AuthConfig",
    "AuthContext",
    "AuthExtension",
    "AuthMiddleware",
    "AuthUser",
    "AuthenticationError",
    "AuthorizationError",
    "SimpleAuthService",
    "get_current_context",
    "get_current_user",
    "requires_auth",
    "requires_permissions",
    "requires_roles",
]
