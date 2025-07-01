# Authentication Patterns

## Current Status: PARTIALLY WORKING

The Cliffracer framework has two authentication systems:

### ✅ Working: SimpleAuthService
The `SimpleAuthService` in `cliffracer.auth.simple_auth` is fully functional:

```python
from cliffracer.auth.simple_auth import SimpleAuthService, AuthConfig

# Create auth service
config = AuthConfig(secret_key="your-secret-key")
auth = SimpleAuthService(config)

# Create user
user = auth.create_user("username", "email@example.com", "password")

# Authenticate
token = auth.authenticate("username", "password")

# Validate token
context = auth.validate_token(token)
```

**Features that work:**
- JWT token generation and validation
- User creation with password hashing
- Role and permission management
- Token expiration
- FastAPI middleware integration

### ❌ Broken: Old Auth Framework
The older `auth_framework` module with decorators like `@requires_auth` is broken:
- Import errors due to missing modules
- Decorators are not functional
- Framework integration disabled

## What to Use

### For New Projects
Use the `SimpleAuthService` which provides:

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer.auth.simple_auth import SimpleAuthService, AuthConfig

class SecureService(CliffracerService):
    def __init__(self):
        config = ServiceConfig(name="secure_service")
        super().__init__(config)
        
        # Setup auth
        auth_config = AuthConfig(secret_key="your-secret-key")
        self.auth = SimpleAuthService(auth_config)
    
    @self.rpc
    async def secure_endpoint(self, token: str, data: dict):
        # Validate token
        context = self.auth.validate_token(token)
        if not context:
            return {"error": "Unauthorized"}
        
        # Access user info
        user = context.user
        return {"message": f"Hello {user.username}"}
```

### For HTTP Services
Use the working FastAPI middleware:

```python
from cliffracer.auth.simple_auth import AuthMiddleware

# In your HTTP service
app.add_middleware(AuthMiddleware, auth_service=auth_service)
```

## Known Limitations

1. The decorators `@requires_auth`, `@requires_roles`, etc. are NOT functional
2. No built-in rate limiting (implement separately)
3. No OAuth2/OIDC support (use external providers)
4. No session management (JWT only)

## Examples

See the e-commerce example for a working implementation of authentication with SimpleAuthService.