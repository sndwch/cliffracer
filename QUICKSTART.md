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
async def on_user_created(self, subject: str, user_id: str) -> None:
    """Listen for user.created events"""
    print(f"Event received: {subject}")
    print(f"User {user_id} was created")

# Wildcard patterns
@listener("user.*", fanout=True)  # Matches user.created, user.deleted, etc.
async def on_any_user_event(self, subject: str, user_id: str) -> None:
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

## Important Notes

1. **Use class-based services** - Inherit from `CliffracerService` or variants
2. **Decorators are imports** - Use `@rpc`, not `@service.rpc`
3. **Methods are async** - All handler methods must be `async def`
4. **Auto-discovery** - Decorated methods are automatically registered on service start

## One service class, plus extensions

There is one service class, `CliffracerService`. Everything optional is a
class attribute:

```python
from cliffracer_logging import LoggingExtension
from cliffracer_metrics import MetricsExtension

class MyService(CliffracerService):
    logging = LoggingExtension()         # structured logging and dispatch timing
    metrics = MetricsExtension()         # counters over the dispatch hooks
```

## Running Services

These are alternative entry points. The first three use the `MyService` class
from the minimal example above.

### Blocking run

For a single service, `run()` keeps the process alive and handles shutdown
signals.

```python
if __name__ == "__main__":
    service = MyService()
    service.run()
```

### Async start and stop

Use `start()` and `stop()` when an async host controls the service's lifetime.
The host owns signal handling; `finally` ensures cleanup when its task is
cancelled or raises.

```python
import asyncio

async def main():
    service = MyService()
    try:
        await service.start()
        await asyncio.Event().wait()  # Serve until the host cancels this task
    finally:
        await service.stop()

if __name__ == "__main__":
    asyncio.run(main())
```

### One service with restart handling

`ServiceRunner` manages one service and uses its configured restart policy.
`MyService` builds its own `ServiceConfig`, so no configuration argument is
needed here.

```python
from cliffracer import ServiceRunner

if __name__ == "__main__":
    runner = ServiceRunner(MyService)
    runner.run_forever()
```

The runner also accepts an optional `ServiceConfig` as its second argument.
For a constructor that accepts configuration, it passes that object to the
constructor. For a self-configuring class such as `MyService`, explicitly set
fields are applied as overrides, preserving the service's own name. Use
`overrides={"name": "another_name"}` when an explicit name override is intended.

### Multiple services in one process

`ServiceOrchestrator` manages a runner for each registered service. This example
runs two distinct services, each with its own RPC address. `health_port=0`
gives each health listener an available port so the listeners do not collide.

```python
from cliffracer import CliffracerService, ServiceConfig, ServiceOrchestrator, rpc

class Orders(CliffracerService):
    @rpc
    async def status(self) -> str:
        return "accepting orders"

class Shipments(CliffracerService):
    @rpc
    async def status(self) -> str:
        return "ready to ship"

if __name__ == "__main__":
    orchestrator = ServiceOrchestrator()
    orchestrator.add_service(Orders, ServiceConfig(name="orders", health_port=0))
    orchestrator.add_service(Shipments, ServiceConfig(name="shipments", health_port=0))
    orchestrator.run_forever()
```

`service.run()` and the runners' `run_forever()` methods are synchronous process
entry points. Inside an existing event loop, use the async lifecycle methods
instead.

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
        super().__init__(ServiceConfig(name="my_service"))

    @rpc
    async def my_handler(self) -> None:
        pass

service = MyService()
```

For more examples, see the `examples/` directory.
