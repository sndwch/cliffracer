# Examples

Working examples of cliffracer. Each runs against a NATS broker on
`nats://localhost:4222`.

## Directories

| | |
|---|---|
| [`basic/`](basic/) | `simple_service.py` and `async_patterns.py` |
| [`ecommerce/`](ecommerce/) | Order, inventory, payment and notification services, plus a load generator |
| [`websocket/`](websocket/) | `notification_service.py` and a `test_client.py` that drives it |
| [`timer/`](timer/) | `@timer` intervals, `@cron` schedules, and timers with metrics |
| [`correlation/`](correlation/) | Correlation ids across service hops |
| [`logging/`](logging/) | `LoggingExtension`, and an ingester that listens on `logs.>` |
| [`validation/`](validation/) | `@validated_listener` |
| [`namespaces/`](namespaces/) | Two apps sharing a service name, isolated by namespace, plus a cross-namespace watcher |
| [`consolidated/`](consolidated/) | One service using `CliffracerService` with `HttpExtension` |
| [`debugging/`](debugging/) | The backdoor console |

Two single files sit at the top level: `rpc_proxy_example.py` for the `RpcProxy`
pattern, and `backdoor_async_test.py`.

## Decorators

`rpc`, `async_rpc`, `listener`, `timer`, `broadcast` and
`validated_listener` are imported from `cliffracer` and used directly:

```python
from cliffracer import CliffracerService, ServiceConfig, rpc, listener

class MyService(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="my_service"))

    @rpc
    async def my_method(self, param: str) -> dict[str, str]:
        return {"result": param}

    @listener("events.*", fanout=True)
    async def on_event(self, subject: str):
        pass
```

HTTP routes and websockets come from the extension that serves them, so those
decorators hang off the declared attribute:

```python
from cliffracer_http import HttpExtension

class MyService(CliffracerService):
    http = HttpExtension()

    @http.get("/items/{item_id}")
    async def get_item(self, item_id: str) -> dict[str, str]:
        return {"id": item_id}
```

`@cron` comes from `cliffracer_cron`.

See [QUICKSTART.md](../QUICKSTART.md) to get a service running.
