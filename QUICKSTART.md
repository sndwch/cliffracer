# Cliffracer Quick Start

## Minimal Service Example

```python
from cliffracer import CliffracerService, ServiceConfig, rpc

class MyService(CliffracerService):
    def __init__(self):
        config = ServiceConfig(
            name="my_service",
            nats_url="nats://localhost:4222"
        )
        super().__init__(config)

    @rpc
    async def hello(self, name: str) -> dict[str, str]:
        """RPC method exposed as: my_service.rpc.hello"""
        return {"message": f"Hello, {name}!"}

if __name__ == "__main__":
    service = MyService()
    service.run()
```

## Common Decorators

### `@rpc` - RPC Handler
```python
@rpc
async def my_method(self, param: str) -> dict[str, str]:
    return {"result": param}
```

### `@listener(pattern)` - Event Listener
```python
from cliffracer import listener

@listener("user.created", fanout=True)
async def on_user_created(self, subject: str, **data):
    """Listen for user.created events"""
    print(f"Event received: {subject}")
    print(f"User {data.get('user_id')} was created")

# Wildcard patterns
@listener("user.*", fanout=True)  # Matches user.created, user.deleted, etc.
async def on_any_user_event(self, subject: str, **data):
    print(f"User event: {subject}")
```

**Publishing events:**
```python
# From within a service method
await self.publish_event("user.created", user_id="123", username="john")
```

### `@timer(interval)` - Scheduled Task
```python
from cliffracer import timer

@timer(interval=60)  # Run every 60 seconds
async def cleanup_task(self):
    print("Running cleanup...")
```

### `@get(path)` and `@post(path)` - HTTP Endpoints
```python
from cliffracer import CliffracerService
from cliffracer_http import HttpExtension

class MyHTTPService(CliffracerService):
    http = HttpExtension(port=8080)

    @http.get("/users/{user_id}")
    async def get_user(self, user_id: str):
        return {"user_id": user_id}

    @http.post("/users")
    async def create_user(self, username: str, email: str):
        return {"status": "created"}
```

## Important Notes

1. **Use class-based services** - Inherit from `CliffracerService` or variants
2. **Decorators are imports** - Use `@rpc`, not `@service.rpc`
3. **Methods are async** - All handler methods must be `async def`
4. **Auto-discovery** - Decorated methods are automatically registered on service start

## One service class, plus extensions

There is one service class, `CliffracerService`. Everything optional is a
class attribute:

```python
class MyService(CliffracerService):
    http = HttpExtension(port=8080)      # REST routes and websockets
    metrics = MetricsExtension()         # counters over the dispatch hooks
```

## Running Services

```python
# Method 1: Blocking run (RECOMMENDED for most cases)
if __name__ == "__main__":
    service = MyService()
    service.run()  # Blocks forever, handles signals

# Method 2: Async start/stop (for advanced control)
import asyncio

async def main():
    service = MyService()
    await service.start()
    # Service is running...
    await asyncio.sleep(3600)  # Keep alive
    await service.stop()

if __name__ == "__main__":
    asyncio.run(main())

# Method 3: Using ServiceRunner (for multiple services)
from cliffracer import ServiceRunner

runner = ServiceRunner(MyService, config)
runner.run_forever()
```

**Common mistake:**
```python
await service.start()  # start() is a coroutine
service.run()          # run() is synchronous and blocks
```

## Making RPC Calls

### New Way: Using RpcProxy (Recommended)

```python
from cliffracer import RpcProxy

class ClientService(CliffracerService):
    my_service = RpcProxy("my_service")

    @rpc
    async def call_hello(self) -> dict[str, str]:
        # Clean, intuitive syntax
        result = await self.my_service.hello(name="World")
        return result
```

### Old Way: Using call_rpc (Still Works)

```python
# From another service or client
result = await client.call_rpc(
    "my_service",      # service name
    "hello",           # method name
    name="World"       # parameters
)
```

**See [examples/rpc_proxy_example.py](examples/rpc_proxy_example.py) for a
runnable version of both forms; it is one of the examples CI runs.**

## Common Mistakes

### Wrong: instance methods as decorators
```python
class MyService(CliffracerService):
    @service.rpc  # WRONG - there is no service.rpc
    async def hello(self, name: str) -> dict[str, str]: ...

    @service.event("user.created")  # WRONG - there is no service.event
    async def on_created(self, subject: str, user_id: str = ""): ...
```

### Right: imported decorators
```python
from cliffracer import CliffracerService, listener, rpc

class MyService(CliffracerService):
    @rpc
    async def hello(self, name: str) -> dict[str, str]:
        return {"greeting": f"Hello, {name}"}

    @listener("user.created", fanout=True)
    async def on_created(self, subject: str, user_id: str = ""):
        print(user_id)
```

### Wrong: decorating on an instance
```python
service = CliffracerService(config)

@service.rpc  # Won't work
async def my_handler() -> None:
    pass
```

### Right: class-based services
```python
class MyService(CliffracerService):
    def __init__(self):
        super().__init__(config)

    @rpc
    async def my_handler(self) -> None:
        pass

service = MyService()
```

For more examples, see the `examples/` directory.
