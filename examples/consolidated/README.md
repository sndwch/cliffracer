# Cliffracer Consolidated Architecture

This directory showcases the new consolidated architecture that replaces the old `BaseNATSService`/`ExtendedNATSService` hierarchy with a clean, mixin-based approach.

## What Changed? 🔄

### Before (Complex Hierarchy)
```
BaseNATSService
├── NATSService  
├── ValidatedNATSService
├── HTTPNATSService
├── WebSocketNATSService  
├── LoggedExtendedService
├── LoggedHTTPService
└── LoggedWebSocketService
```

### After (Clean Composition)
```
CliffracerService (core)
├── Mixins (composable features)
│   ├── ValidationMixin
│   ├── HTTPMixin
│   ├── WebSocketMixin
│   ├── BroadcastMixin
│   └── PerformanceMixin
└── Pre-configured Classes
    ├── NATSService
    ├── ValidatedNATSService  
    ├── HTTPNATSService
    ├── WebSocketNATSService
    ├── HighPerformanceService
    └── FullFeaturedService
```

## Key Improvements 🚀

### ✅ **Reduced Complexity**
- **From 8+ service classes → 2-3 core classes + mixins**
- Clear separation of concerns
- Composable functionality

### ✅ **Unified Decorators** 
All decorators now in one place:
```python
from cliffracer import (
    rpc, timer, validated_rpc, broadcast, listener,
    get, post, monitor_performance, retry, cache_result,
    robust_rpc, scheduled_task
)
```

### ✅ **Comprehensive Error Handling**
Unified exception hierarchy:
```python
from cliffracer import (
    ServiceError, RPCError, ValidationError, 
    HTTPError, TimerError, ErrorHandler
)
```

### ✅ **Better Performance Integration**
Performance features are now mixins that can be applied to any service.

## Service Types Available

### Basic Services
```python
from cliffracer import NATSService, ServiceConfig

class MyService(NATSService):
    def __init__(self):
        config = ServiceConfig(name="my_service")
        super().__init__(config)
    
    @rpc
    async def hello(self):
        return "Hello, World!"
```

### Validated Services
```python
from cliffracer import ValidatedNATSService, validated_rpc
from pydantic import BaseModel

class UserRequest(BaseModel):
    username: str
    email: str

class UserService(ValidatedNATSService):
    @validated_rpc(UserRequest)
    async def create_user(self, request: UserRequest):
        return {"user_id": f"user_{request.username}"}
```

### HTTP Services
```python
from cliffracer import HTTPNATSService, get, post

class APIService(HTTPNATSService):
    def __init__(self):
        super().__init__(config, host="0.0.0.0", port=8080)
    
    @get("/users/{user_id}")
    async def get_user(self, user_id: str):
        return {"user_id": user_id}
    
    @post("/users")
    async def create_user(self, user_data: dict):
        return {"status": "created"}
```

### WebSocket Services
```python
from cliffracer import WebSocketNATSService, websocket_handler

class RealtimeService(WebSocketNATSService):
    @websocket_handler("/notifications")
    async def handle_notifications(self, websocket):
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"Echo: {data}")
```

### High-Performance Services
```python
from cliffracer import HighPerformanceService

class FastService(HighPerformanceService):
    def __init__(self):
        super().__init__(
            config,
            enable_connection_pooling=True,
            enable_batch_processing=True,
            enable_metrics=True
        )
```

### Full-Featured Services
```python
from cliffracer import FullFeaturedService

class ComprehensiveService(FullFeaturedService):
    def __init__(self):
        super().__init__(
            config,
            host="0.0.0.0", 
            port=8080,
            enable_connection_pooling=True,
            enable_batch_processing=True,
            enable_metrics=True
        )
    
    @rpc
    @timer(interval=30)
    @monitor_performance()
    async def comprehensive_method(self):
        # All features available!
        pass
```

## Advanced Decorator Patterns

### Robust RPC with Built-in Features
```python
@robust_rpc(schema=UserRequest, max_attempts=3, monitor=True)
async def create_user(self, request: UserRequest):
    # Automatic validation, retry, and monitoring!
    return {"user_id": f"user_{request.username}"}
```

### Scheduled Tasks with Monitoring  
```python
@scheduled_task(interval=60, eager=True, monitor=True, max_attempts=2)
async def health_check(self):
    # Timer + retry + monitoring in one decorator!
    await self.check_database_connection()
```

### Composed Decorators
```python
@compose_decorators(
    rpc,
    monitor_performance(),
    retry(max_attempts=3),
    cache_result(ttl_seconds=30)
)
async def expensive_operation(self):
    # RPC + monitoring + retry + caching!
    return await self.complex_calculation()
```

## Error Handling

### Exception Hierarchy
```python
try:
    result = await service.call_rpc("user_service", "create_user", **data)
except ValidationError as e:
    # Handle validation errors
    print(f"Invalid data: {e}")
except RPCError as e:
    # Handle RPC-specific errors  
    print(f"RPC failed: {e}")
except ServiceError as e:
    # Handle general service errors
    print(f"Service error: {e}")
```

### Error Context Manager
```python
async with ErrorHandler("User creation failed", RPCError):
    user = await service.create_user(user_data)
    # Automatic error wrapping and handling!
```

## Migration Guide

### From Old Architecture
```python
# OLD
from cliffracer import BaseNATSService, HTTPNATSService
from cliffracer.logging import LoggedExtendedService

# NEW  
from cliffracer import NATSService, HTTPNATSService
# LoggedExtendedService is now handled by LoggingMixin
```

### Decorator Changes
```python
# OLD - scattered across modules
from cliffracer.core.base_service import rpc
from cliffracer.core.extended_service import validated_rpc
from cliffracer.core.timer import timer

# NEW - all in one place
from cliffracer import rpc, validated_rpc, timer
```

## Benefits Summary

1. **🏗️ Cleaner Architecture**: Mixin-based composition vs inheritance hell
2. **📦 Unified Imports**: All decorators and exceptions in one place  
3. **🔧 Better Maintainability**: Clear separation of concerns
4. **⚡ Performance**: Built-in optimizations via mixins
5. **🛡️ Error Handling**: Comprehensive exception hierarchy
6. **🔄 Backward Compatibility**: Legacy aliases still work
7. **🎯 Developer Experience**: Intuitive API with powerful features

The consolidated architecture makes Cliffracer more maintainable, performant, and developer-friendly while keeping all the powerful features that make it a production-ready microservices framework!