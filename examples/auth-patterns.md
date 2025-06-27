# Authentication and RBAC Patterns

This example demonstrates comprehensive Role-Based Access Control (RBAC) implementation for NATS microservices with multiple authentication patterns and JWT token management.

## Overview

The authentication patterns example (`example_auth_patterns.py`) showcases:

- **JWT token authentication** with automatic validation
- **Role-based access control** with granular permissions
- **Multiple authentication patterns** (explicit context, context variables, decorators)
- **HTTP middleware integration** for FastAPI services
- **Context propagation** across service boundaries
- **Permission and role decorators** for method-level security

## Core Components

### 1. Roles and Permissions

```python
class Permission(str, Enum):
    READ_USERS = "read:users"
    WRITE_USERS = "write:users"
    DELETE_USERS = "delete:users"
    READ_ORDERS = "read:orders"
    WRITE_ORDERS = "write:orders"
    DELETE_ORDERS = "delete:orders"
    ADMIN_ACCESS = "admin:*"

class Role(str, Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    USER = "user"
    GUEST = "guest"

# Role-to-permission mapping
ROLE_PERMISSIONS = {
    Role.ADMIN: {Permission.ADMIN_ACCESS},
    Role.MANAGER: {Permission.READ_USERS, Permission.WRITE_USERS, 
                   Permission.READ_ORDERS, Permission.WRITE_ORDERS},
    Role.USER: {Permission.READ_USERS, Permission.READ_ORDERS, Permission.WRITE_ORDERS},
    Role.GUEST: {Permission.READ_USERS}
}
```

### 2. Request Context

```python
@dataclass
class RequestContext:
    user_id: str
    username: str
    roles: List[Role]
    permissions: Set[Permission]
    session_id: str
    request_id: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    
    def has_permission(self, permission: Permission) -> bool:
        return Permission.ADMIN_ACCESS in self.permissions or permission in self.permissions
    
    def has_role(self, role: Role) -> bool:
        return role in self.roles
```

## Authentication Patterns

### Pattern 1: Explicit Context Parameter

Pass context explicitly to every authenticated method:

```python
class UserServiceExplicit(AuthenticatedService):
    @validated_rpc(CreateUserRequest, UserResponse)
    @requires_auth(permissions=[Permission.WRITE_USERS])
    async def create_user(self, request: CreateUserRequest, context: RequestContext) -> UserResponse:
        # Context is explicitly passed and validated
        print(f"User {context.username} creating user: {request.username}")
        
        # Use context for audit trail
        user_data = {
            "user_id": f"user_{self.user_counter}",
            "username": request.username,
            "created_by": context.user_id
        }
        
        # Propagate context to other services
        await self.call_rpc_with_context(
            "audit_service",
            "log_user_creation",
            context=context,
            user_id=user_data["user_id"]
        )
        
        return UserResponse(**user_data)
```

### Pattern 2: Context Variables (Automatic Propagation)

Use Python's `contextvars` for automatic context propagation:

```python
class UserServiceAuto(AuthenticatedService):
    @authenticated_rpc(permissions=[Permission.WRITE_USERS])
    async def create_user(self, request: CreateUserRequest) -> UserResponse:
        # Get context automatically from context variable
        context = get_current_context()
        print(f"User {context.username} creating user: {request.username}")
        
        # Context automatically propagated in RPC calls
        await self.call_rpc_with_context(
            "audit_service",
            "log_user_creation",
            user_id=user_data["user_id"]
        )
        
        return UserResponse(**user_data)
```

### Pattern 3: Decorator-Based Authorization

Use decorators for granular permission control:

```python
class OrderService(AuthenticatedService):
    @require_permission(Permission.READ_ORDERS)
    async def get_orders(self, user_id: str = None, limit: int = 10) -> List[dict]:
        context = get_current_context()
        
        # Role-based filtering
        if context.has_role(Role.ADMIN) or context.has_role(Role.MANAGER):
            # Admins and managers can see all orders
            target_user_id = user_id
        else:
            # Regular users can only see their own orders
            target_user_id = context.user_id
        
        return self.filter_orders(target_user_id, limit)
    
    @require_role(Role.ADMIN)
    async def delete_order(self, order_id: str) -> dict:
        # Only admins can delete orders
        context = get_current_context()
        print(f"Admin {context.username} deleting order: {order_id}")
        
        return self.remove_order(order_id)
```

## HTTP Authentication Middleware

### FastAPI Integration

```python
class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token_service: TokenService):
        super().__init__(app)
        self.token_service = token_service
    
    async def dispatch(self, request: Request, call_next):
        # Extract token from Authorization header
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
            
            try:
                # Validate token and set context
                context = self.token_service.validate_token(token)
                current_context.set(context)
                request.state.context = context
                
            except AuthenticationError:
                raise HTTPException(status_code=401, detail="Invalid token")
        
        response = await call_next(request)
        return response
```

### FastAPI Dependencies

```python
# Dependency for getting current context
def get_current_context(credentials: HTTPAuthorizationCredentials = Depends(security)) -> RequestContext:
    context = current_context.get()
    if not context:
        raise HTTPException(status_code=401, detail="Authentication required")
    return context

# Permission requirement factory
def require_permissions(*permissions: Permission):
    def permission_dependency(context: RequestContext = Depends(get_current_context)) -> RequestContext:
        if not any(context.has_permission(perm) for perm in permissions):
            raise HTTPException(status_code=403, detail=f"Missing permissions: {permissions}")
        return context
    return permission_dependency

# Role requirement factory
def require_roles(*roles: Role):
    def role_dependency(context: RequestContext = Depends(get_current_context)) -> RequestContext:
        if not context.has_any_role(list(roles)):
            raise HTTPException(status_code=403, detail=f"Missing roles: {roles}")
        return context
    return role_dependency
```

## JWT Token Service

```python
class TokenService:
    def __init__(self, secret_key: str):
        self.secret_key = secret_key
    
    def create_token(self, context: RequestContext, expires_in_hours: int = 24) -> str:
        """Create JWT token from context"""
        payload = context.to_dict()
        payload['exp'] = datetime.utcnow() + timedelta(hours=expires_in_hours)
        
        return jwt.encode(payload, self.secret_key, algorithm="HS256")
    
    def validate_token(self, token: str) -> RequestContext:
        """Validate JWT token and return context"""
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=["HS256"])
            return RequestContext.from_dict(payload)
        except jwt.ExpiredSignatureError:
            raise AuthenticationError("Token expired")
        except jwt.InvalidTokenError:
            raise AuthenticationError("Invalid token")
```

## Running the Example

### Prerequisites

```bash
# Start NATS server
docker run -d --name nats-server -p 4222:4222 -p 8222:8222 nats:alpine -js -m 8222
```

### Run the Demo

```bash
# Run the authentication patterns demo
python example_auth_patterns.py
```

### Expected Output

```
=== RBAC Authentication Patterns Demo ===

1. AUTHENTICATION
----------------------------------------
Admin token: eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...
Manager token: eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...
User token: eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...

Admin permissions: ['admin:*']
Manager permissions: ['read:users', 'write:users', 'read:orders', 'write:orders']
User permissions: ['read:users', 'read:orders', 'write:orders']

2. ROLE-BASED ACCESS CONTROL
----------------------------------------

Admin Operations:
✓ Admin created user: employee
✓ Admin deleted user: employee
✓ Admin viewed 2 audit logs

Manager Operations:
✓ Manager created user: employee
✓ Manager viewed 1 orders
✗ Manager cannot delete users: Role required: admin

User Operations:
✓ User created order: order_1
✓ User viewed 1 of their orders
✗ User cannot create users: Missing permissions: [Permission.WRITE_USERS]

3. PERMISSION VALIDATION
----------------------------------------
Admin has admin access: True
Manager has write users: True
User has write users: False
User has read orders: True
```

## HTTP Service Example

```python
class UserHTTPService(AuthenticatedHTTPService):
    def __init__(self, config: ServiceConfig, token_service: TokenService):
        super().__init__(config, token_service, port=8001)
        
        # Login endpoint
        @self.app.post("/auth/login")
        async def login(credentials: dict):
            username = credentials["username"]
            password = credentials["password"]
            
            # Validate credentials (mock implementation)
            if self.validate_user(username, password):
                context = self.create_user_context(username)
                token = self.token_service.create_token(context)
                
                return {
                    "access_token": token,
                    "token_type": "bearer",
                    "user": {
                        "username": username,
                        "role": context.roles[0].value
                    }
                }
            else:
                raise HTTPException(status_code=401, detail="Invalid credentials")
        
        # Protected endpoint requiring specific permission
        @self.app.post("/users")
        async def create_user(
            user_data: dict,
            context: RequestContext = Depends(require_permissions(Permission.WRITE_USERS))
        ):
            user = self.create_user_logic(user_data, context)
            return user
        
        # Protected endpoint requiring admin role
        @self.app.delete("/users/{user_id}")
        async def delete_user(
            user_id: str,
            context: RequestContext = Depends(require_roles(Role.ADMIN))
        ):
            result = self.delete_user_logic(user_id, context)
            return result
```

## Usage Examples

### Authentication Flow

```bash
# 1. Login to get token
curl -X POST "http://localhost:8001/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}'

# Response:
# {
#   "access_token": "eyJ0eXAiOiJKV1Q...",
#   "token_type": "bearer",
#   "user": {"username": "admin", "role": "admin"}
# }

# 2. Use token for authenticated requests
curl -X POST "http://localhost:8001/users" \
  -H "Authorization: Bearer eyJ0eXAiOiJKV1Q..." \
  -H "Content-Type: application/json" \
  -d '{"username": "newuser", "email": "new@example.com"}'
```

### Permission Errors

```bash
# Try accessing admin endpoint as regular user
curl -X DELETE "http://localhost:8001/users/user_1" \
  -H "Authorization: Bearer <user_token>"

# Response:
# {
#   "detail": "Missing required roles: ['admin']"
# }
```

## Key Security Features

### 1. Automatic Permission Checking

```python
@require_permission(Permission.WRITE_USERS)
async def create_user(self, request):
    # Permission automatically validated before method execution
    pass
```

### 2. Context Propagation

```python
# Context automatically propagated across service calls
await self.call_rpc_with_context(
    "audit_service",
    "log_action",
    action="user_created"
    # No need to manually pass context
)
```

### 3. Role-Based Data Filtering

```python
@require_permission(Permission.READ_ORDERS)
async def get_orders(self, user_id: str = None):
    context = get_current_context()
    
    # Automatic role-based filtering
    if not (context.has_role(Role.ADMIN) or context.has_role(Role.MANAGER)):
        user_id = context.user_id  # Force users to see only their data
    
    return self.fetch_orders(user_id)
```

### 4. Audit Trail Integration

```python
@authenticated_rpc(permissions=[Permission.DELETE_USERS])
async def delete_user(self, user_id: str):
    context = get_current_context()
    
    # Automatic audit logging with context
    await self.call_rpc_with_context(
        "audit_service",
        "log_user_deletion",
        target_user_id=user_id,
        performed_by=context.user_id,
        session_id=context.session_id
    )
    
    return self.delete_user_logic(user_id)
```

## Best Practices Demonstrated

1. **Principle of Least Privilege**: Users only get minimal required permissions
2. **Defense in Depth**: Multiple layers of security (JWT, permissions, roles)
3. **Audit Trail**: All privileged actions are logged with context
4. **Context Isolation**: Each request has isolated authentication context
5. **Automatic Validation**: Decorators handle authentication/authorization checks
6. **Secure Defaults**: Endpoints are secure by default, require explicit permission grants

## Next Steps

1. **[Database Integration](database-integration.md)** - Store users and roles in database
2. **[Session Management](session-management.md)** - Advanced session handling
3. **[OAuth Integration](oauth-integration.md)** - External identity providers
4. **[Audit Logging](audit-logging.md)** - Comprehensive audit trail system
5. **[Production Deployment](../deployment/security.md)** - Security considerations for production

## Security Considerations

- **Token Storage**: Store JWT secrets securely (environment variables, key management)
- **Token Expiration**: Set appropriate expiration times and implement refresh tokens
- **HTTPS Only**: Always use HTTPS in production
- **Input Validation**: Validate all user inputs with Pydantic schemas
- **Rate Limiting**: Implement rate limiting for authentication endpoints
- **Audit Logging**: Log all authentication and authorization events
