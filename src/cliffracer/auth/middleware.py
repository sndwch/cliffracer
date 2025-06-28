"""
HTTP Authentication Middleware for FastAPI services
"""

from .framework import (
    AuthenticationError,
    Permission,
    RequestContext,
    Role,
    TokenService,
    current_context,
)
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware

from cliffracer import HTTPNATSService, ServiceConfig


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware to extract and validate authentication from HTTP requests"""

    def __init__(self, app, token_service: TokenService, exclude_paths: list[str] = None):
        super().__init__(app)
        self.token_service = token_service
        self.exclude_paths = exclude_paths or ["/health", "/docs", "/openapi.json"]

    async def dispatch(self, request: Request, call_next):
        # Skip authentication for excluded paths
        if request.url.path in self.exclude_paths:
            return await call_next(request)

        try:
            # Extract token from Authorization header
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                raise HTTPException(
                    status_code=401, detail="Missing or invalid authorization header"
                )

            token = auth_header[7:]  # Remove "Bearer " prefix

            # Validate token and get context
            context = self.token_service.validate_token(token)

            # Set context for this request
            current_context.set(context)

            # Add context to request state for FastAPI dependency injection
            request.state.context = context

            response = await call_next(request)
            return response

        except AuthenticationError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail="Authentication error") from e
        finally:
            # Clear context
            current_context.set(None)


# FastAPI dependency for getting current context
security = HTTPBearer()


def get_current_context(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> RequestContext:
    """FastAPI dependency to get current authentication context"""
    context = current_context.get()
    if not context:
        raise HTTPException(status_code=401, detail="Authentication required")
    return context


def require_permissions(*permissions: Permission):
    """FastAPI dependency factory for permission requirements"""

    def permission_dependency(
        context: RequestContext = Depends(get_current_context),
    ) -> RequestContext:
        if not any(context.has_permission(perm) for perm in permissions):
            raise HTTPException(
                status_code=403,
                detail=f"Missing required permissions: {[p.value for p in permissions]}",
            )
        return context

    return permission_dependency


def require_roles(*roles: Role):
    """FastAPI dependency factory for role requirements"""

    def role_dependency(context: RequestContext = Depends(get_current_context)) -> RequestContext:
        if not context.has_any_role(list(roles)):
            raise HTTPException(
                status_code=403, detail=f"Missing required roles: {[r.value for r in roles]}"
            )
        return context

    return role_dependency


# Example authenticated HTTP service


class AuthenticatedHTTPService(HTTPNATSService):
    """HTTP service with authentication middleware"""

    def __init__(self, config: ServiceConfig, token_service: TokenService, **kwargs):
        super().__init__(config, **kwargs)
        self.token_service = token_service

        # Add authentication middleware
        self.app.add_middleware(AuthMiddleware, token_service=token_service)

        # Add authentication endpoints
        self._setup_auth_routes()

    def _setup_auth_routes(self):
        """Setup authentication-related routes"""

        @self.app.post("/auth/login")
        async def login(credentials: dict):
            """Login endpoint"""
            try:
                # This would typically validate against a user database
                username = credentials.get("username")
                password = credentials.get("password")

                if not username or not password:
                    raise HTTPException(status_code=400, detail="Username and password required")

                # Mock user validation (replace with real user service)
                user_db = {
                    "admin": {"password": "admin123", "role": Role.ADMIN},
                    "user": {"password": "user123", "role": Role.USER},
                }

                user = user_db.get(username)
                if not user or user["password"] != password:
                    raise HTTPException(status_code=401, detail="Invalid credentials")

                # Create context
                from datetime import datetime, timedelta

                context = RequestContext(
                    user_id=f"user_{username}",
                    username=username,
                    roles=[user["role"]],
                    permissions=set(),
                    session_id=f"http_session_{datetime.utcnow().timestamp()}",
                    request_id=f"http_req_{datetime.utcnow().timestamp()}",
                    created_at=datetime.utcnow(),
                    expires_at=datetime.utcnow() + timedelta(hours=24),
                )

                # Generate token
                token = self.token_service.create_token(context)

                return {
                    "access_token": token,
                    "token_type": "bearer",
                    "expires_in": 86400,  # 24 hours
                    "user": {
                        "id": context.user_id,
                        "username": username,
                        "role": user["role"].value,
                    },
                }

            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail="Login failed") from e

        @self.app.get("/auth/me")
        async def get_current_user(context: RequestContext = Depends(get_current_context)):
            """Get current user info"""
            return {
                "user_id": context.user_id,
                "username": context.username,
                "roles": [role.value for role in context.roles],
                "permissions": [perm.value for perm in context.permissions],
                "session_id": context.session_id,
            }

        @self.app.post("/auth/logout")
        async def logout(context: RequestContext = Depends(get_current_context)):
            """Logout endpoint"""
            # In a real implementation, you'd invalidate the token/session
            return {"message": "Logged out successfully"}


# Example usage in a real service
class UserHTTPService(AuthenticatedHTTPService):
    """User management service with HTTP endpoints"""

    def __init__(self, config: ServiceConfig, token_service: TokenService):
        super().__init__(config, token_service, port=8001)
        self.users = {}
        self.user_counter = 0

        self._setup_user_routes()

    def _setup_user_routes(self):
        """Setup user management routes"""

        @self.app.post("/users")
        async def create_user(
            user_data: dict,
            context: RequestContext = Depends(require_permissions(Permission.WRITE_USERS)),
        ):
            """Create user - requires write permission"""
            self.user_counter += 1
            user_id = f"user_{self.user_counter}"

            user = {
                "id": user_id,
                "username": user_data["username"],
                "email": user_data["email"],
                "created_by": context.user_id,
                "created_at": context.created_at.isoformat(),
            }

            self.users[user_id] = user

            # Could call other services here with context propagation
            # await self.call_rpc_with_context("audit_service", "log_action", ...)

            return user

        @self.app.get("/users/{user_id}")
        async def get_user(
            user_id: str,
            context: RequestContext = Depends(require_permissions(Permission.READ_USERS)),
        ):
            """Get user - requires read permission"""
            user = self.users.get(user_id)
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            # Users can only see their own data unless admin
            if user_id != context.user_id and not context.has_role(Role.ADMIN):
                raise HTTPException(status_code=403, detail="Access denied")

            return user

        @self.app.delete("/users/{user_id}")
        async def delete_user(
            user_id: str, context: RequestContext = Depends(require_roles(Role.ADMIN))
        ):
            """Delete user - admin only"""
            if user_id not in self.users:
                raise HTTPException(status_code=404, detail="User not found")

            user = self.users.pop(user_id)
            return {"deleted": True, "user": user}

        @self.app.get("/users")
        async def list_users(
            limit: int = 10, context: RequestContext = Depends(get_current_context)
        ):
            """List users - role-based filtering"""
            users = list(self.users.values())

            # Regular users can only see their own profile
            if not (context.has_role(Role.ADMIN) or context.has_role(Role.MANAGER)):
                users = [u for u in users if u["id"] == context.user_id]

            return {"users": users[:limit], "total": len(users)}


# Example client code
async def demo_http_auth():
    """Demonstrate HTTP authentication"""
    import httpx

    base_url = "http://localhost:8001"

    # Login
    login_response = await httpx.AsyncClient().post(
        f"{base_url}/auth/login", json={"username": "admin", "password": "admin123"}
    )

    if login_response.status_code == 200:
        token = login_response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Create user
        create_response = await httpx.AsyncClient().post(
            f"{base_url}/users",
            json={"username": "newuser", "email": "new@example.com"},
            headers=headers,
        )

        print(f"Create user response: {create_response.json()}")

        # Get current user info
        me_response = await httpx.AsyncClient().get(f"{base_url}/auth/me", headers=headers)

        print(f"Current user: {me_response.json()}")


if __name__ == "__main__":
    # Example of running the authenticated HTTP service

    token_service = TokenService("your-secret-key")
    config = ServiceConfig(name="user_http_service")

    # This would normally be run with the service runner
    print("Example authenticated HTTP service setup complete")
    print("Run with ServiceRunner to start the actual service")
