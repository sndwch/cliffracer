# Cliffracer Architecture

How the pieces fit together: one service class, extensions declared as class
attributes, and NATS underneath. For the API itself see
[api-reference.md](api-reference.md); for writing an extension see
[extensions.md](extensions.md).

## The service

There is one service class, `CliffracerService`. It owns the NATS connection,
discovers decorated handlers at `start()`, dispatches messages through a hook
chain, and serves `GET /health` and `GET /info` on its own listener.

Everything else is an extension.

## Extensions

Optional functionality is composed by **declaring extensions as class
attributes**.

```python
from cliffracer import CliffracerService
from cliffracer_http import HttpExtension
from cliffracer_metrics import MetricsExtension

class MyService(CliffracerService):
    """Service with HTTP endpoints and metrics"""

    http = HttpExtension(port=8080)
    metrics = MetricsExtension()
```

The attribute name is how you reach the extension (`self.http.port`) and how
its decorators are spelled (`@http.get("/users")`). Each extension ships as its
own distribution:

| distribution | provides |
|---|---|
| `cliffracer-http` | FastAPI app, REST routes, websockets, correlation middleware |
| `cliffracer-auth` | JWT auth, roles and permissions |
| `cliffracer-logging` | structured logging, correlation logging, log-to-NATS |
| `cliffracer-metrics` | in-process counters over the dispatch hooks |
| `cliffracer-otel` | OpenTelemetry distributed tracing and W3C context propagation |
| `cliffracer-kv` | NATS JetStream Key-Value and Object Store integration with bucket TTL |
| `cliffracer-resilience` | circuit breaking, sliding-window rate limiting, and resilient RPC |
| `cliffracer-backdoor` | the async debug backdoor |
| `cliffracer-cron` | `@cron` handlers (a `Timer` subclass, so core's timer discovery runs it — no extension to declare) |
| `cliffracer-faststream` | FastStream broker hosting, resilient ACK/DLQ routing, and shutdown drain |

Installing a distribution and declaring its extension is what loads it. See
[extensions.md](extensions.md) for the hook contract and how to write one.

### Why extensions rather than mixins

- **Installing is opting in**: an extension runs, contributes to `/health`
  and appears in a stack trace once you install it and declare it
- **No MRO to reason about**: a mixin hierarchy decides behaviour by class
  order; an extension is a named attribute you can point at
- **Independent release**: each extension versions and ships on its own
- **A boundary that can be tested**: the hook contract is small enough to
  assert against, hook by hook

## Messaging

Cliffracer uses core NATS for request/reply and publish/subscribe, and
JetStream when a listener asks for durability.

### RPC

Handlers are typed from their annotations. Annotate every parameter and the
return, or the service refuses to start and names the handler.

```python
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc


class Payment(BaseModel):
    transaction_id: str
    status: str


# Service A (caller) — uses call_rpc or RpcProxy
class OrderService(CliffracerService):
    @rpc
    async def process_order(self, total: float) -> dict[str, str]:
        payment = await self.call_rpc("payment_service", "process_payment", amount=total)
        return {"order_id": "123", "payment": payment["transaction_id"]}


# Service B (handler)
class PaymentService(CliffracerService):
    @rpc
    async def process_payment(self, amount: float) -> Payment:
        return Payment(transaction_id="tx_456", status="completed")
```

### Events

```python
from pydantic import BaseModel

from cliffracer import CliffracerService, listener, rpc


class User(BaseModel):
    email: str
    name: str


class UserService(CliffracerService):
    @rpc
    async def create_user(self, user: User) -> User:
        await self.publish_event("user.created", **user.model_dump())
        return user


class EmailService(CliffracerService):
    @listener("user.created", fanout=True)
    async def on_user_created(self, subject: str, user: User):
        await self.send_welcome_email(user.email)
```

A listener declares either `fanout=True` (every replica receives it) or a
durable name (one replica receives it); a service whose listeners declare
neither refuses to start.

## Correlation

A correlation id travels with a request: generated on the way in or taken from
a header, propagated across service boundaries, and available in any handler
through `CorrelationContext.get()`. `cliffracer-logging` puts it on log lines;
`HttpExtension` adds `CorrelationMiddleware` to its app so an HTTP request
carries one too.

## Health

Every service serves `GET /health` — core does it without a web framework, and
`HttpExtension` takes it over on its own app when installed.

```text
GET /health -> {
  "service": "user_service",
  "status": "healthy",          # healthy | unhealthy | connecting | disconnected | stopped
  "timestamp": "2026-09-06T18:04:11.512Z",
  "nats_connected": true,
  "features": {...}             # handler counts by kind
}
```

The status code carries the same answer as the field: 200 when `status` is
`healthy`, 503 in every other state, on both the core listener and the
`HttpExtension` app. A probe that reads only the code is enough.

`status` is the field to probe, and its vocabulary is five values, not two:
`connecting` means broker connection dial is in progress, `disconnected` means
the broker connection is closed for good, and `stopped` means before `start()`
or after `stop()`. Declared dependencies add a
`dependencies` block, and `unhealthy_dependencies` names the failures so a
probe reading one line does not have to walk it.

The listener binds `127.0.0.1` by default. A container healthcheck runs in the
container's own network namespace and reaches loopback; anything reading
`/health` from OUTSIDE the container — a Kubernetes probe, a reverse proxy —
needs `health_host="0.0.0.0"`.

## Metrics

`cliffracer-metrics` collects in-process counters over the dispatch hooks and
contributes them to `/health` and `/info`. Export them from your own service:
scrape the endpoint, or read `self.metrics` and write them wherever you keep
metrics.

## Configuration

`ServiceConfig` is constructed in code, or per service through the CLI's
`--config` YAML. The full field list is in
[api-reference.md](api-reference.md#serviceconfig), which is generated from the
model.

```python
from cliffracer import CliffracerService, ServiceConfig

config = ServiceConfig(
    name="user_service",
    nats_url="nats://localhost:4222",
    health_port=8000,
)

service = CliffracerService(config)
```

Extension settings come from the environment under their own prefixes —
`CLIFFRACER_HTTP_*`, `CLIFFRACER_BACKDOOR_*`. `AuthConfig` takes its
`secret_key` as an argument.

## Testing

Two suites, and the markers `pyproject.toml` declares:

| | |
|---|---|
| `tests/unit/` | `unit`, no external dependencies |
| `tests/integration/` | `integration`, and `nats_required` where a broker is needed |

`slow` marks a long-running test in either. `$CLIFFRACER_TEST_NATS_URL` moves
the broker the whole suite dials; without it, tests marked `nats_required` skip
and the rest run. `load-testing/` contains locust scripts, which the suite does
not run.

## Deployment

```dockerfile
FROM python:3.11-slim

COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --no-dev

COPY src/ ./src/

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s \
  CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "src.your_service"]
```

The healthcheck above runs inside the container, so the default loopback bind
is enough. A Kubernetes probe does not:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: user-service
spec:
  replicas: 3
  selector:
    matchLabels:
      app: user-service
  template:
    metadata:
      labels:
        app: user-service
    spec:
      containers:
      - name: user-service
        image: cliffracer/user-service:1.0.0
        ports:
        - containerPort: 8000
        env:
        - name: NATS_URL
          value: "nats://nats-service:4222"
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 10
```

kubelet reaches the pod's IP rather than its loopback, so a service probed this
way sets `health_host="0.0.0.0"`.

A service that cannot reach its broker at startup logs the address and raises
`NatsError` once `connect_timeout` expires.
