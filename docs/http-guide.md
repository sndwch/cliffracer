# HTTP and REST API Guide

This guide covers how to build HTTP/REST APIs using Cliffracer's HTTP integration.

## Overview

Cliffracer provides seamless HTTP integration through FastAPI, allowing you to expose your microservices via REST APIs while maintaining NATS-based communication between services.

## Basic HTTP Service

### Using HTTPMixin

The simplest way to add HTTP capabilities to your service is using the `HTTPMixin`:

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.mixins import HTTPMixin

class MyHTTPService(CliffracerService, HTTPMixin):
    def __init__(self):
        config = ServiceConfig(
            name="my_http_service",
            nats_url="nats://localhost:4222"
        )
        super().__init__(config)
        self._http_port = 8080  # Set HTTP port
        
        # The mixin automatically creates self.app (FastAPI instance)
        self._setup_routes()
    
    def _setup_routes(self):
        """Define HTTP routes"""
        @self.app.get("/hello")
        async def hello():
            return {"message": "Hello from Cliffracer!"}
        
        @self.app.post("/users")
        async def create_user(name: str, email: str):
            # Call internal RPC method
            result = await self.create_user_internal(name, email)
            return result
    
    @self.rpc
    async def create_user_internal(self, name: str, email: str) -> dict:
        """Internal method callable via NATS RPC"""
        return {"id": "123", "name": name, "email": email}
```

### Using HTTP Decorators

Cliffracer provides convenient decorators for HTTP endpoints:

```python
from cliffracer import CliffracerService
from cliffracer.core.decorators import get, post, put, delete

class APIService(CliffracerService, HTTPMixin):
    def __init__(self):
        super().__init__(ServiceConfig(name="api_service"))
        self._http_port = 8080
    
    @get("/users/{user_id}")
    async def get_user(self, user_id: str):
        """GET /users/{user_id}"""
        user = await self.fetch_user_from_db(user_id)
        return {"user": user}
    
    @post("/users")
    async def create_user(self, user: UserModel):
        """POST /users with JSON body"""
        created = await self.save_user_to_db(user)
        return {"id": created.id, "status": "created"}
    
    @put("/users/{user_id}")
    async def update_user(self, user_id: str, user: UserModel):
        """PUT /users/{user_id}"""
        updated = await self.update_user_in_db(user_id, user)
        return {"status": "updated"}
    
    @delete("/users/{user_id}")
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

class APIService(CliffracerService, HTTPMixin):
    @post("/users", response_model=UserResponse)
    async def create_user(self, user: UserModel) -> UserResponse:
        # Automatic validation of input and output
        created = await self.user_repository.create(user)
        return UserResponse(**created.dict())
```

## Middleware Integration

### Correlation ID Middleware

Automatically included when using HTTPMixin:

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

class APIService(CliffracerService, HTTPMixin):
    def __init__(self):
        super().__init__(config)
        self._http_port = 8080
        
        # Add CORS middleware
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        
        # Custom middleware
        @self.app.middleware("http")
        async def add_process_time_header(request: Request, call_next):
            start_time = time.time()
            response = await call_next(request)
            process_time = time.time() - start_time
            response.headers["X-Process-Time"] = str(process_time)
            return response
```

## Authentication

Integrate authentication with HTTP endpoints:

```python
from fastapi import Depends, HTTPException, Header
from cliffracer.auth.simple_auth import SimpleAuthService, AuthConfig

class SecureAPIService(CliffracerService, HTTPMixin):
    def __init__(self):
        super().__init__(config)
        self._http_port = 8080
        
        # Setup auth
        auth_config = AuthConfig(secret_key="your-secret-key")
        self.auth = SimpleAuthService(auth_config)
    
    async def get_current_user(self, authorization: str = Header(...)):
        """Dependency to get current user from JWT token"""
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Invalid authorization")
        
        token = authorization.split(" ")[1]
        context = self.auth.validate_token(token)
        
        if not context:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        return context.user
    
    @get("/profile")
    async def get_profile(self, current_user=Depends(get_current_user)):
        """Protected endpoint requiring authentication"""
        return {
            "username": current_user.username,
            "email": current_user.email,
            "roles": current_user.roles
        }
    
    @post("/login")
    async def login(self, username: str, password: str):
        """Login endpoint"""
        token = self.auth.authenticate(username, password)
        if not token:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        return {"access_token": token, "token_type": "bearer"}
```

## Error Handling

Proper error handling for HTTP endpoints:

```python
from fastapi import HTTPException
from cliffracer.core.exceptions import ValidationError, AuthenticationError

class APIService(CliffracerService, HTTPMixin):
    def __init__(self):
        super().__init__(config)
        
        # Global exception handler
        @self.app.exception_handler(ValidationError)
        async def validation_exception_handler(request: Request, exc: ValidationError):
            return JSONResponse(
                status_code=400,
                content={"detail": str(exc), "type": "validation_error"}
            )
        
        @self.app.exception_handler(AuthenticationError)
        async def auth_exception_handler(request: Request, exc: AuthenticationError):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"}
            )
    
    @get("/users/{user_id}")
    async def get_user(self, user_id: str):
        try:
            user = await self.fetch_user(user_id)
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            return user
        except DatabaseError as e:
            raise HTTPException(status_code=500, detail="Database error")
```

## OpenAPI Documentation

FastAPI automatically generates OpenAPI documentation:

```python
class APIService(CliffracerService, HTTPMixin):
    def __init__(self):
        super().__init__(config)
        self._http_port = 8080
        
        # Customize OpenAPI
        self.app.title = "My Microservice API"
        self.app.description = "Cliffracer-powered microservice"
        self.app.version = "1.0.0"
    
    @post("/users", 
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
class UserService(CliffracerService, HTTPMixin):
    # HTTP endpoint - thin layer
    @post("/users")
    async def create_user_http(self, user: UserModel):
        result = await self.create_user_internal(user.dict())
        return {"id": result["id"], "status": "created"}
    
    # Business logic - reusable via RPC
    @self.rpc
    async def create_user_internal(self, user_data: dict):
        # Actual business logic here
        # Can be called via HTTP or NATS RPC
        return await self.repository.create(user_data)
```

### 2. Use Proper Status Codes
```python
from fastapi import status

@post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(self, user: UserModel):
    return {"id": "123"}

@delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(self, user_id: str):
    await self.delete_user_internal(user_id)
    # Return nothing for 204
```

### 3. Pagination for Lists
```python
from typing import List, Optional

@get("/users")
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

@post("/send-notification")
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
from typing import List, Optional
from pydantic import BaseModel, Field
from fastapi import Depends, HTTPException, Header, status

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.mixins import HTTPMixin
from cliffracer.core.decorators import get, post, put, delete
from cliffracer.database import SecureRepository
from cliffracer.auth.simple_auth import SimpleAuthService, AuthConfig

class TodoItem(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    completed: bool = False

class TodoResponse(BaseModel):
    id: str
    title: str
    description: Optional[str]
    completed: bool
    created_at: datetime
    user_id: str

class TodoService(CliffracerService, HTTPMixin):
    def __init__(self):
        config = ServiceConfig(name="todo_service")
        super().__init__(config)
        self._http_port = 8080
        
        # Setup auth
        auth_config = AuthConfig(secret_key="your-secret-key-here")
        self.auth = SimpleAuthService(auth_config)
        
        # Setup repository
        self.todos = SecureRepository(TodoModel)
    
    async def get_current_user(self, authorization: str = Header(...)):
        """Extract user from JWT token"""
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Invalid authorization")
        
        token = authorization.split(" ")[1]
        context = self.auth.validate_token(token)
        
        if not context:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        return context.user
    
    @get("/todos", response_model=List[TodoResponse])
    async def list_todos(
        self,
        skip: int = 0,
        limit: int = 100,
        current_user=Depends(get_current_user)
    ):
        """List user's todos with pagination"""
        todos = await self.todos.find_by_field("user_id", current_user.id)
        return todos[skip:skip + limit]
    
    @post("/todos", response_model=TodoResponse, status_code=status.HTTP_201_CREATED)
    async def create_todo(
        self,
        todo: TodoItem,
        current_user=Depends(get_current_user)
    ):
        """Create a new todo"""
        todo_dict = todo.dict()
        todo_dict["user_id"] = current_user.id
        created = await self.todos.create(TodoModel(**todo_dict))
        return TodoResponse(**created.dict())
    
    @put("/todos/{todo_id}", response_model=TodoResponse)
    async def update_todo(
        self,
        todo_id: str,
        todo: TodoItem,
        current_user=Depends(get_current_user)
    ):
        """Update a todo"""
        existing = await self.todos.get(todo_id)
        if not existing or existing.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Todo not found")
        
        updated = await self.todos.update(todo_id, todo.dict())
        return TodoResponse(**updated.dict())
    
    @delete("/todos/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_todo(
        self,
        todo_id: str,
        current_user=Depends(get_current_user)
    ):
        """Delete a todo"""
        existing = await self.todos.get(todo_id)
        if not existing or existing.user_id != current_user.id:
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
# Set a different port
self._http_port = 8081
```

### CORS Issues
```python
# Add CORS middleware (shown above)
self.app.add_middleware(CORSMiddleware, allow_origins=["*"])
```

### Large Request Bodies
```python
# Increase max request size
from fastapi import Request

@self.app.post("/upload")
async def upload(request: Request):
    # Handle large uploads
    body = await request.body()
    if len(body) > 10_000_000:  # 10MB
        raise HTTPException(status_code=413, detail="Request too large")
```

## Next Steps

- Check out the [WebSocket Guide](websocket-guide.md) for real-time features
- See [Authentication Guide](../auth-guide.md) for more auth patterns
- Review [Examples](../examples/) for complete applications