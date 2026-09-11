# HTTP and REST API Guide

This guide covers how to build HTTP/REST APIs using Cliffracer's HTTP integration.

## Overview

Cliffracer provides seamless HTTP integration through FastAPI, allowing you to expose your microservices via REST APIs while maintaining NATS-based communication between services.

## Basic HTTP Service

### Declaring the extension

HTTP comes from the `cliffracer-http` distribution. Declare `HttpExtension` as
a **class attribute**; the attribute name is how the routes are spelled and how
you reach the app.

```python
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer_http import HttpExtension

class MyHTTPService(CliffracerService):
    http = HttpExtension(port=8080)

    def __init__(self):
        config = ServiceConfig(
            name="my_http_service",
            nats_url="nats://localhost:4222"
        )
        super().__init__(config)

    @http.get("/hello")
    async def hello(self):
        return {"message": "Hello from Cliffracer!"}

    @http.post("/users")
    async def create_user_http(self, name: str, email: str):
        # Delegate to the RPC method
        return await self.create_user_internal(name=name, email=email)

    @rpc
    async def create_user_internal(self, name: str, email: str) -> dict[str, str]:
        """Internal method callable via NATS RPC"""
        return {"id": "123", "name": name, "email": email}
```

`@http.get`, `@http.post`, `@http.put` and `@http.delete` are methods on the
extension instance, which is what ties a route to the app that serves it.
`self.http.app` is the FastAPI instance if you need it directly.

`http = HttpExtension(port=8080)` also settles where the probes are served.
Core serves `GET /health` and `GET /info` on its own listener without a web
framework; when this extension is present it serves both on its app instead and
the core listener stands down, so the service has one port, not two.

Either way `/health` answers 200 when `status` is `healthy` and 503 otherwise,
so `curl -f` and a compose healthcheck work against the extension's port
without parsing the body.

### The four verbs

```python
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig
from cliffracer_http import HttpExtension

class UserModel(BaseModel):
    name: str
    email: str

class APIService(CliffracerService):
    http = HttpExtension(port=8080)

    def __init__(self):
        super().__init__(ServiceConfig(name="api_service"))

    @http.get("/users/{user_id}")
    async def get_user(self, user_id: str):
        """GET /users/{user_id}"""
        user = await self.fetch_user_from_db(user_id)
        return {"user": user}

    @http.post("/users")
    async def create_user(self, user: UserModel):
        """POST /users with JSON body"""
        created = await self.save_user_to_db(user)
        return {"id": created.id, "status": "created"}

    @http.put("/users/{user_id}")
    async def update_user(self, user_id: str, user: UserModel):
        """PUT /users/{user_id}"""
        updated = await self.update_user_in_db(user_id, user)
        return {"status": "updated"}

    @http.delete("/users/{user_id}")
    async def delete_user(self, user_id: str):
        """DELETE /users/{user_id}"""
        await self.delete_user_from_db(user_id)
        return {"status": "deleted"}
```

## Request/Response Models

Use Pydantic models for request/response validation:

```python
from pydantic import BaseModel, Field
from typing import Optional

class UserModel(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., pattern=r'^[\w\.-]+@[\w\.-]+\.\w+$')
    age: Optional[int] = Field(None, ge=0, le=150)

class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    created_at: datetime

class APIService(CliffracerService):
    http = HttpExtension(port=8080)

    @http.post("/users", response_model=UserResponse)
    async def create_user(self, user: UserModel) -> UserResponse:
        # Automatic validation of input and output
        created = await self.user_repository.create(user)
        return UserResponse(**created)
```

## Middleware Integration

### Correlation ID Middleware

`HttpExtension` installs `CorrelationMiddleware` on its app itself. There is
nothing to add:

```python
# Correlation IDs are automatically extracted from headers:
# - X-Correlation-ID
# - X-Request-ID
# - X-Trace-ID

# And propagated to all NATS calls made within the request
```

### Custom Middleware

Add your own FastAPI middleware:

```python
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware

from cliffracer import CliffracerService
from cliffracer_http import HttpExtension

class APIService(CliffracerService):
    http = HttpExtension(port=8080)

    async def on_startup(self):
        # `self.http.app` does not exist until the extension's setup() runs.
        # on_startup is after setup and before the server starts, which is the
        # window where the app can still be changed. In __init__ it is None.
        await super().on_startup()
        self.http.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        # Custom middleware
        @self.http.app.middleware("http")
        async def add_process_time_header(request: Request, call_next):
            start_time = time.time()
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = str(process_time)
            return response
```

## Authentication

Auth is the `cliffracer-auth` distribution. Two things authenticate
separately, each with its own mechanism:

**NATS handlers** — declare `AuthExtension` and decorate the handler. The
extension reads the `authorization: Bearer <token>` header off the message,
validates it, and makes the caller reachable from the handler body:

```python
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer_auth import (
    AuthConfig,
    AuthExtension,
    SimpleAuthService,
    get_current_user,
    requires_roles,
)

auth_service = SimpleAuthService(AuthConfig(secret_key="a-secret-key-of-at-least-32-characters"))


class Caller(BaseModel):
    username: str
    roles: list[str]


class SecureService(CliffracerService):
    auth = AuthExtension(auth_service)

    def __init__(self):
        super().__init__(ServiceConfig(name="secure_service"))

    @rpc
    async def whoami(self) -> Caller:
        user = get_current_user()          # the caller, inside the handler
        return Caller(username=user.username, roles=sorted(user.roles))

    @rpc
    @requires_roles("admin")
    async def admin_only(self) -> dict[str, bool]:
        return {"ok": True}
```

**Decorator order matters**: `@rpc` outermost, `@requires_roles` beneath it.
`@rpc` is what registers the subject; the guard has to sit between it and your
function body.

**Declaring `AuthExtension` makes the whole service authenticated-only.** The
refusal is in `worker_setup`, before dispatch, so it applies to every handler on
the service — `@rpc`, `@listener`, decorated or not. `@requires_roles` narrows
an already-authenticated caller, and declaring `AuthExtension` is what turns
authentication on.

What a caller gets back:

| caller | reply |
|---|---|
| valid token, has the role | the handler's result |
| valid token, wrong role | `error: "Required roles: ('admin',)"` — authorization, and the caller *was* identified |
| no token, any handler | `error: "refused: unauthenticated"`, and the handler never runs |
| expired or revoked token | the same `refused: unauthenticated` — not "forbidden": the caller has not established who they are |
| the issuer raises or is unreachable | `refused: unauthenticated`. It fails closed, so *a handler that reaches its body has been authenticated* is unconditionally true |

Pinned by `packages/cliffracer-auth/tests/test_auth_context_reaches_the_handler.py::test_a_requires_roles_handler_runs_for_a_caller_with_the_role`
and its negative in the same file, both driving a real dispatch with a real
token. `test_auth_decorators.py` sets the contextvar itself, so do not read it
as evidence about the extension.

**HTTP routes** authenticate through FastAPI. `AuthExtension` guards NATS
dispatch, so a request arriving on `self.http.app` is guarded by an ordinary
FastAPI dependency, reaching the same `SimpleAuthService`:

```python
from fastapi import Depends, Header, HTTPException

from cliffracer import CliffracerService, ServiceConfig
from cliffracer_auth import AuthConfig, SimpleAuthService
from cliffracer_http import HttpExtension

auth_service = SimpleAuthService(AuthConfig(secret_key="a-secret-key-of-at-least-32-characters"))

async def current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization")
    context = auth_service.validate_token(authorization.split(" ", 1)[1])
    if not context:
        raise HTTPException(status_code=401, detail="Invalid token")
    return context.user

class SecureAPIService(CliffracerService):
    http = HttpExtension(port=8080)

    def __init__(self):
        super().__init__(ServiceConfig(name="secure_api"))

    @http.get("/profile")
    async def get_profile(self, user=Depends(current_user)):
        return {"username": user.username, "email": user.email, "roles": sorted(user.roles)}

    @http.post("/login")
    async def login(self, username: str, password: str):
        token = auth_service.authenticate(username, password)
        if not token:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return {"access_token": token, "token_type": "bearer"}
```

Declare the dependency as a module-level function. FastAPI resolves `Depends`
against the callable it is given, and the class body runs before any instance
exists, so `self` is out of reach there.

`SimpleAuthService`'s user store is a dict in the service process, so it lives
as long as the process. It suits tests and small deployments.

## Error Handling

Proper error handling for HTTP endpoints:

```python
from fastapi import HTTPException

from cliffracer import CliffracerService
from cliffracer.core.exceptions import AuthenticationError, ValidationError
from cliffracer_http import HttpExtension

class APIService(CliffracerService):
    http = HttpExtension(port=8080)

    async def on_startup(self):
        await super().on_startup()

        # Global exception handler
        @self.http.app.exception_handler(ValidationError)
        async def validation_exception_handler(request: Request, exc: ValidationError):
            return JSONResponse(
                status_code=400,
                content={"detail": str(exc), "type": "validation_error"}
            )
        
        @self.http.app.exception_handler(AuthenticationError)
        async def auth_exception_handler(request: Request, exc: AuthenticationError):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"}
            )
    
    @http.get("/users/{user_id}")
    async def get_user(self, user_id: str):
        try:
            user = await self.fetch_user(user_id)
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            return user
        except MyStoreError:
            # Your own exception type, defined alongside the store that raises
            # it. See the Error Handling section of api-reference.md.
            raise HTTPException(status_code=500, detail="Database error")
```

## OpenAPI Documentation

FastAPI automatically generates OpenAPI documentation:

Both halves are pinned in
`packages/cliffracer-http/tests/test_http_extension.py`:
`test_a_caller_supplied_title_reaches_the_app`,
`test_without_a_title_the_service_name_is_still_the_default`, and
`test_other_fastapi_kwargs_still_pass_through` for `description` and `version`.


```python
class APIService(CliffracerService):
    # Keyword arguments other than host and port are forwarded to FastAPI(),
    # so the OpenAPI metadata is set right here. A title given here wins; give
    # none and the app is titled "<service name> API".
    http = HttpExtension(
        port=8080,
        title="My Microservice API",
        description="Cliffracer-powered microservice",
        version="1.0.0",
    )
    
    @http.post("/users", 
          summary="Create a new user",
          description="Creates a new user in the system",
          response_description="The created user",
          tags=["users"])
    async def create_user(self, user: UserModel):
        """This docstring appears in the OpenAPI docs"""
        return await self.create_user_internal(user)
```

Access the docs at:
- Swagger UI: `http://localhost:8080/docs`
- ReDoc: `http://localhost:8080/redoc`
- OpenAPI JSON: `http://localhost:8080/openapi.json`

## Best Practices

### 1. Separate HTTP from Business Logic
```python
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer_http import HttpExtension


class UserModel(BaseModel):
    name: str
    email: str


class User(BaseModel):
    id: str
    name: str
    email: str


class UserService(CliffracerService):
    http = HttpExtension(port=8080)

    # HTTP endpoint - thin layer
    @http.post("/users")
    async def create_user_http(self, user: UserModel):
        created = await self.create_user_internal(user)
        return {"id": created.id, "status": "created"}

    # Business logic - reusable via RPC. The model travels over NATS, which is
    # what makes this callable from another service rather than only from the
    # route above.
    @rpc
    async def create_user_internal(self, user: UserModel) -> User:
        return await self.repository.create(user)
```

### 2. Use Proper Status Codes
```python
from fastapi import status

@http.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(self, user: UserModel):
    return {"id": "123"}

@http.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(self, user_id: str):
    await self.delete_user_internal(user_id)
    # Return nothing for 204
```

### 3. Pagination for Lists
```python
from typing import List, Optional

@http.get("/users")
async def list_users(
    self,
    skip: int = 0,
    limit: int = 100,
    sort: Optional[str] = None
):
    users = await self.fetch_users(skip=skip, limit=limit, sort=sort)
    return {
        "items": users,
        "total": len(users),
        "skip": skip,
        "limit": limit
    }
```

### 4. Background Tasks
```python
from fastapi import BackgroundTasks

@http.post("/send-notification")
async def send_notification(
    self,
    email: str,
    background_tasks: BackgroundTasks
):
    # Return immediately
    background_tasks.add_task(self.send_email_internal, email)
    return {"status": "notification queued"}

async def send_email_internal(self, email: str):
    # This runs in the background
    await self.email_service.send(email)
```

## Complete Example

Here's a complete HTTP service example:

```python
from datetime import datetime

from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from cliffracer import CliffracerService, ServiceConfig
from cliffracer_auth import AuthConfig, SimpleAuthService
from cliffracer_http import HttpExtension


class TodoItem(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    completed: bool = False


class TodoResponse(BaseModel):
    id: str
    title: str
    description: str | None
    completed: bool
    created_at: datetime
    user_id: str


auth = SimpleAuthService(AuthConfig(secret_key="a-secret-key-of-at-least-32-characters"))


async def current_user(authorization: str = Header(...)):
    """A module-level dependency. FastAPI resolves Depends against the callable
    it is handed, so a method here would make `self` a query parameter."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization")
    context = auth.validate_token(authorization.split(" ", 1)[1])
    if not context:
        raise HTTPException(status_code=401, detail="Invalid token")
    return context.user


class TodoService(CliffracerService):
    http = HttpExtension(port=8080)

    def __init__(self):
        super().__init__(ServiceConfig(name="todo_service"))
        # Persistence is yours to choose:
        # e.g. a SQLAlchemy async session factory, or an asyncpg pool.
        self.todos = TodoStore(pool)

    @http.get("/todos", response_model=list[TodoResponse])
    async def list_todos(self, skip: int = 0, limit: int = 100, user=Depends(current_user)):
        """List the caller's todos, paginated"""
        todos = await self.todos.find_by_field("user_id", user.user_id)
        return todos[skip : skip + limit]

    @http.post("/todos", response_model=TodoResponse, status_code=status.HTTP_201_CREATED)
    async def create_todo(self, todo: TodoItem, user=Depends(current_user)):
        """Create a new todo"""
        created = await self.todos.create(todo.model_dump() | {"user_id": user.user_id})
        return TodoResponse(**created)

    @http.put("/todos/{todo_id}", response_model=TodoResponse)
    async def update_todo(self, todo_id: str, todo: TodoItem, user=Depends(current_user)):
        """Update a todo"""
        existing = await self.todos.get(todo_id)
        if not existing or existing["user_id"] != user.user_id:
            raise HTTPException(status_code=404, detail="Todo not found")
        updated = await self.todos.update(todo_id, todo.model_dump())
        return TodoResponse(**updated)

    @http.delete("/todos/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_todo(self, todo_id: str, user=Depends(current_user)):
        """Delete a todo"""
        existing = await self.todos.get(todo_id)
        if not existing or existing["user_id"] != user.user_id:
            raise HTTPException(status_code=404, detail="Todo not found")
        await self.todos.delete(todo_id)


if __name__ == "__main__":
    service = TodoService()
    service.run()
```

## Running HTTP Services

```bash
# Start the service
python todo_service.py

# Access the API
curl http://localhost:8080/todos

# View API docs
open http://localhost:8080/docs
```

## Troubleshooting

### Port Already in Use
```python
# The port is the extension's, set where it is declared
http = HttpExtension(port=8081)
```

### CORS Issues
```python
# Add CORS middleware in on_startup (shown above)
self.http.app.add_middleware(CORSMiddleware, allow_origins=["*"])
```

### Large Request Bodies
```python
# Bound the body in the handler, or at your reverse proxy. Those are the two
# places the limit exists.
from fastapi import Request

@http.post("/upload")
async def upload(self, request: Request):
    # Handle large uploads
    body = await request.body()
    if len(body) > 10_000_000:  # 10MB
        raise HTTPException(status_code=413, detail="Request too large")
```

## Next Steps

- Check out the [WebSocket Guide](websocket-guide.md) for real-time features
- Read `packages/cliffracer-auth/README.md` for the auth extension in full
- Review [Examples](../examples/) for complete applications