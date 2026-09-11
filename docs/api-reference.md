# API Reference

Quick reference for the main Cliffracer classes and methods.

## Core Classes

### CliffracerService

The main service class for building microservices.

```python
from cliffracer import CliffracerService, ServiceConfig

class MyService(CliffracerService):
    def __init__(self):
        config = ServiceConfig(name="my_service")
        super().__init__(config)
```

**Key Methods:**
- `run()` - Start the service
- `stop()` - Stop the service gracefully
- `publish_event(subject: str, **kwargs)` - Publish event
- `call_rpc(service: str, method: str, *, namespace: str | None = None, **kwargs)` - Call RPC method

**Decorators (imported from `cliffracer`):**
- `@rpc` - Define RPC method
- `@listener("pattern", fanout=True)` - Subscribe to events
- `@timer(interval=N)` - Run a method every N seconds
- `@cron("expr", tz="UTC")` - Run a method on a cron schedule
- `@idempotent(key="...")` - Deduplicated event publishing via JetStream

### Scheduling: `@timer` vs `@cron`

`@timer` runs at a fixed interval; `@cron` runs at wall-clock times described by a cron
expression. Both are discovered automatically and started/stopped with the service.

```python
from cliffracer import CliffracerService, ServiceConfig, timer
from cliffracer_cron import cron

class Jobs(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="jobs"))

    @timer(interval=30)                      # every 30 seconds
    async def poll(self):
        ...

    @timer(interval=3600, eager=True)        # hourly, and once on startup
    async def warm_cache(self):
        ...

    @cron("0 9 * * *")                       # 09:00 UTC every day
    async def daily_report(self):
        ...

    @cron("*/15 * * * *", tz="America/Chicago")  # every 15 min, Chicago time
    async def sync(self):
        ...

    @cron("@hourly")                         # named schedules work too
    async def rollup(self):
        ...
```

`@cron(expression, tz="UTC", eager=False)`:
- `expression` — any cron expression (`"min hour dom month dow"`) or named schedule
  (`@hourly`, `@daily`, `@weekly`, ...). Validated when the decorator is applied — a bad
  expression raises `ValueError` immediately, not silently at runtime.
- `tz` — IANA timezone the expression is evaluated in (default `"UTC"`). Invalid names raise `ValueError`.
- `eager` — if `True`, also run once on service start.

Semantics match `@timer`: fire-and-forget, in-process, **not** persisted — a service that is
down when a scheduled time passes does not "catch up" on the missed run when it restarts.

### Idempotent Publishing: `@idempotent`

`@idempotent` extracts an idempotency key from method arguments or hashes the payload, binding it to ambient context for outgoing JetStream event publishes.

```python
from cliffracer import CliffracerService, ServiceConfig, idempotent

class OrderService(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="orders", jetstream_enabled=True))

    @idempotent(key="order_id")
    async def process_order(self, order_id: str, amount: float):
        """Publish with deterministic Nats-Msg-Id for JetStream deduplication."""
        await self.publish_event("order.processed", order_id=order_id, amount=amount)

    @idempotent(hash_payload=True)
    async def sync_inventory(self, items: list[dict]):
        """Publish with SHA-256 hash of domain arguments."""
        await self.publish_event("inventory.synced", items=items)
```

`@idempotent(key=None, hash_payload=False)`:
- `key` — parameter name (e.g. `"order_id"`), dotted attribute path (e.g. `"order.id"`), or callable extracting a key string from `(*args, **kwargs)`.
- `hash_payload` — if `True`, computes a SHA-256 hash across domain payload arguments (excluding framework metadata). Defaults to `True` when `@idempotent` is used bare without arguments.

Outgoing `publish_event` calls attach the extracted key as a subject-scoped `Nats-Msg-Id` header.

> **Note**: `@idempotent` relies entirely on JetStream's native message deduplication window via the `Nats-Msg-Id` header. It operates without requiring `cliffracer-kv` or an external cache.

### Message Validation

Validate inbound messages against a pydantic model.

**RPC (request/reply)** — annotate the handler. A parameter annotated with a
pydantic model is validated against it and the handler receives the model;
responses use one envelope whatever the handler declared:
- success → `{"success": true, "result": ...}`
- validation failure → `{"success": false, "error": "validation failed", "details": [...field errors...]}`
- refusal / error → `{"error": "...", "timestamp": "...", "correlation_id": "..."}` (handler exceptions include `traceback`; unknown methods omit `correlation_id`)

On the caller side, `call_rpc` raises `cliffracer.core.exceptions.RPCError` for error
responses (and `RPCTimeoutError` on timeout). The field-level errors are available on the
exception's `.details`:
```python
from cliffracer.core.exceptions import RPCError
try:
    await self.users.create_user(username="x")   # missing email
except RPCError as e:
    print(e.details)   # [{"loc": ["email"], "msg": "field required"}, ...]
```

**Events (fire-and-forget)** — `@validated_listener(pattern, Schema, on_invalid=None)`:

```python
from pydantic import BaseModel
from cliffracer import CliffracerService, validated_listener

class OrderCreated(BaseModel):
    order_id: str
    amount: float

class OrderService(CliffracerService):
    @validated_listener("orders.created", OrderCreated, fanout=True)
    async def on_order(self, message: OrderCreated):   # already validated
        ...
```

Invalid messages have no caller to reject to, so they are handled by `on_invalid`:
- `"deadletter"` (default): the raw payload + field-level errors are republished to the
  service's DLQ subject (`dlq.{service}`, configurable via `ServiceConfig.dlq_subject`),
  so nothing is lost and a monitor can subscribe to `dlq.*`.
- `"drop"`: the errors are logged at WARNING and the message is discarded (never silent).

Set the default for a service with `ServiceConfig(default_on_invalid="drop")`; override per
handler with `@validated_listener(..., on_invalid="deadletter")`.

**Shared-schema contract (recommended):** put the pydantic model in a module both the
publisher and the consumer import, and have the publisher call `Model(**x).model_dump()`
before publishing — then most invalid messages never leave the sender.

Note: transport is core NATS (no redelivery); dead-lettering to a subject is the pattern.
Like all events, validated listeners are fire-and-forget and not persisted.

### Describing a service, and calling one from a client

A service answers its own description on `{service}.describe`: every `@rpc`
handler, its parameters and return as structural type references, a hash per
method and one for the whole description. `cliffracer.introspect.describe(cls,
service=..., version=...)` computes the same value from the class without a
broker, so the offline and the live answer are the same bytes.

`ServiceClient` is the base a typed client extends. It encodes each argument
through the annotation the method declares, validates the reply against the
declared return type, and maps the reply envelope to one of
`RpcValidationError` (with pydantic's `details`), `RpcUnknownMethod`,
`RpcRefused`, `RpcNoResponders`, `RpcTimeout` or `ClientError`. On the first call
it compares its per-method hashes against the running service and raises
`ClientOutOfDate`, naming the methods that moved.

```python
from cliffracer import ServiceClient

class OrdersClient(ServiceClient):
    SERVICE = "orders"
    SIGNATURES = {"create": "sha256:..."}

    async def create(self, order: Order) -> Receipt:
        return await self._call("create", {"order": self._encode(order, Order)}, Receipt)
```

### Generating a client

`cliffracer-generate-client` writes the client for a service to a file.

```text
cliffracer-generate-client --class myapp.warehouse:Warehouse --service warehouse \
    --version 1.0.0 --out warehouse_client.py
cliffracer-generate-client --service warehouse --nats-url nats://localhost:4222 \
    --header authorization="bearer $TOKEN" --out warehouse_client.py
```

`--class MODULE:CLASS` describes an importable class in process; without it the
command asks a running service. Both go through one emitter, so the two forms
produce the same bytes. `--header` is repeatable and is what a service behind
`AuthExtension` needs, since it refuses an unauthenticated describe like any
other message. Without `--out` the client goes to stdout.

| exit | meaning |
|---|---|
| 0 | a client was written |
| 2 | the broker answered and no such service did |
| 3 | no broker at that address |
| 4 | the service cannot be described, or its description cannot be emitted |
| 5 | the class named by `--class` could not be imported |

Exit 4 covers a handler that is not annotated and a model whose module a
generated client could not import, such as one defined in `__main__`. No
failure writes a file.

### Correlation IDs

A correlation ID is tracked per request via a `contextvars.ContextVar` and propagated
two ways: in the JSON body **and** the NATS message headers of every `call_rpc`,
`call_async`, and `publish_event` (receivers read the body first, headers as a fallback —
so the ID survives even a non-Cliffracer consumer). Because it's a context variable, it
also flows automatically into `asyncio.create_task(...)` spawned inside a handler (Python
copies the context at task creation). The one exception is work pushed to a **thread**
(`run_in_executor`) — context variables do not cross threads, so capture and re-set the ID
manually there.

### Namespaces

Run multiple apps on one NATS server with the same service names by giving each app a
namespace. Set `ServiceConfig(namespace="app1")` (a single subject token). When set, it
prefixes **everything the service owns** — RPC, async, events/broadcasts, and the DLQ
subject. Default is `None` (no prefix; unchanged behavior).

```python
svc = MyService(ServiceConfig(name="user_service", namespace="app1"))
# subscribes to app1.user_service.rpc.*, publishes events as app1.<subject>, DLQ app1.dlq.user_service
```

**Calling across namespaces.** `call_rpc` and `RpcProxy` default to the caller's own
namespace; pass `namespace=` to target another:

```python
await self.call_rpc("user_service", "get_user", namespace="app2", user_id="u1")
other = RpcProxy("user_service", namespace="app2")
```

**Queue groups (RPC/async).** RPC and async subscriptions join a NATS queue group keyed by
namespace+service, so multiple replicas of the same service load-balance (one handles each
call) instead of all replying. Events stay fan-out (no queue group). Consequence: two
*distinct apps* reusing the same service name **without** namespaces would load-balance with
each other — give distinct apps **distinct namespaces** to isolate them. Same-name services
in the same namespace are treated as replicas.

**Broadcasts across namespaces.** A broadcast subscriber stays namespace-local by default;
pass `cross_namespace=True` to receive that subject from every namespace:

```python
@listener("orders.created", fanout=True, cross_namespace=True)   # subscribes to *.orders.created
async def on_order(self, subject, **data):
    ...
```

**Publishing is namespace-local, by design.** `publish_event` always prefixes the
service's own namespace and takes no `namespace=` parameter, unlike `call_rpc`.
Crossing a namespace boundary on the event path is the *subscriber's* opt-in, via
`@listener(..., cross_namespace=True)`. This asymmetry is deliberate: a service
publishing into another project's namespace would be claiming subjects it does
not own, which is the coupling namespaces exist to prevent. A cross-namespace
request/response protocol is therefore built as two one-way flows, each service
publishing under its own namespace.

A namespace disambiguates subjects. Any service that knows the name can
target another namespace, so for a security boundary use NATS accounts:
separate credentials per app, enforced by the broker.

### ServiceConfig

Configuration for services.

Every field, its default, and what it does. This table is generated from
`ServiceConfig.model_fields` by `tools/gen_service_config_table.py`; a guard
fails if it drifts.

<!-- service-config-fields -->
| field | default | description |
|---|---|---|
| `name` | `required` | Service name. Forms the RPC subject prefix and the default DLQ subject, so it must be unique on the broker. |
| `nats_url` | `'nats://localhost:4222'` | Broker to connect to. Prefer the auth fields below to embedding credentials here. |
| `nats_user` | `None` | Username, if the broker requires user/password auth. |
| `nats_password` | `None` | Password for `nats_user`. Set both or neither. |
| `nats_token` | `None` | Token auth, as an alternative to user/password. |
| `nats_credentials_file` | `None` | Path to a NATS `.creds` file, for NGS or an operator-mode broker. |
| `max_reconnect_attempts` | `-1` | How many times to retry a lost connection. `-1` retries forever; a finite value ends in a permanent close, which is what `exit_on_closed` then decides about. |
| `reconnect_time_wait` | `2` | Seconds between reconnect attempts. |
| `connect_timeout` | `30.0` | Seconds the FIRST connect may take before the service gives up, logs the address and raises `NatsError`. `None` disables the timeout: `max_reconnect_attempts` governs the first connect, and `-1` and `0` both mean forever. Reconnects are unaffected. |
| `shutdown_timeout` | `30.0` | Maximum seconds allowed to drain active tasks during shutdown before cancellation. |
| `exit_on_closed` | `True` | On a permanent close, log at ERROR and initiate graceful shutdown (`await self.stop()`). Set `False` to keep the service running and poll `status: "disconnected"` on the health endpoint. |
| `request_timeout` | `30.0` | Seconds an outbound RPC waits for its reply before raising. |
| `serialization_format` | `'json'` | Payload serialization format: `'json'` or `'msgpack'`. |
| `expose_internal_errors` | `False` | Whether to include full exception tracebacks in wire RPC error responses. |
| `max_rpc_concurrency` | `None` | Maximum concurrent RPC handlers executing simultaneously. |
| `max_event_concurrency` | `None` | Maximum concurrent event handlers executing simultaneously. |
| `max_async_rpc_concurrency` | `None` | Maximum concurrent async fire-and-forget RPC handlers executing simultaneously. |
| `jetstream_enabled` | `False` | Enable JetStream. Every `jetstream_*` field below is ignored while this is `False`. |
| `jetstream_streams` | `[]` | Streams this service declares at startup, as `StreamSpec` entries. A durable listener whose subject no stream here covers fails startup. |
| `jetstream_max_deliver` | `5` | Redelivery limit before a message goes to the DLQ. Write-once per durable: raising it after the consumer exists leaves the server terminating at the old value, before the DLQ boundary. |
| `jetstream_ack_wait` | `30.0` | Seconds the server waits for an ack before redelivering. Write-once per durable. |
| `jetstream_max_ack_pending` | `64` | In-flight bound: unacked messages the server will hand this service at once. Write-once per durable. |
| `jetstream_pull_batch` | `8` | How many messages a pull consumer fetches per request. Small on purpose; `jetstream_max_ack_pending` is what bounds in-flight work. |
| `jetstream_pull_timeout` | `5.0` | Seconds a pull fetch waits before returning empty. |
| `jetstream_nak_backoff` | `1.0` | Base seconds before a naked message is redelivered. Doubles per delivery. |
| `jetstream_max_backoff` | `60.0` | Ceiling for the doubling `jetstream_nak_backoff`. |
| `jetstream_update_streams` | `False` | Let startup rewrite an existing stream whose subjects have changed. Off by default: two services declaring the same stream differently would flap it on every boot. |
| `idempotent_publishing` | `False` | Whether to automatically generate idempotency keys from domain payloads. |
| `on_connect` | `None` | Called after the initial connection and after each reconnection -- once per connection, so it is safe for one-time setup. |
| `on_disconnect` | `None` | Called when the connection drops, before reconnection is attempted. |
| `on_error` | `None` | Called with the exception for connection-level errors nats-py reports. |
| `version` | `'0.1.0'` | Reported on the health endpoint and in service metadata. |
| `health_listener` | `True` | Serve the built-in health endpoint. Off means nothing listens and container healthchecks reading it will fail; see `CliffracerService.health_listener` for what it serves. |
| `health_host` | `'127.0.0.1'` | Interface the health endpoint binds. A container healthcheck runs in the container's own network namespace, so loopback is enough for it. Set `'0.0.0.0'` when something outside the container reads `/health` -- a reverse proxy, or a probe on another host. |
| `health_port` | `8000` | Port for the health endpoint. Two services in one process need different values here. |
| `log_level` | `'INFO'` | Minimum level for this service's own logging. |
| `description` | `None` | Free text, reported in service metadata. |
| `namespace` | `None` | Prefix token for subject isolation between apps on a shared broker. A single subject token: no `.`, `*`, `>` or whitespace. |
| `auto_restart` | `True` | Let `ServiceOrchestrator` restart this service if it stops. |
| `restart_delay` | `1.0` | Seconds the orchestrator waits before restarting. |
| `default_on_invalid` | `'deadletter'` | What to do with a message that fails validation: `deadletter` publishes it to `dlq_subject`, `drop` discards it. A handler can override per listener. |
| `dlq_subject` | `'dlq.{service}'` | Where dead-lettered messages go. `{service}` is substituted with `name`. |
<!-- /service-config-fields -->

## Extensions

Optional functionality is declared as class attributes. Each extension ships as
its own distribution, and declaring one is what loads it. The attribute name is
how you reach the extension and how its decorators are spelled.

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer_http import HttpExtension

class APIService(CliffracerService):
    http = HttpExtension(port=8080)

    @http.get("/users/{user_id}")
    async def get_user(self, user_id: str) -> dict[str, str]:
        return {"user_id": user_id}
```

| distribution | attribute type | provides |
|---|---|---|
| `cliffracer-http` | `HttpExtension`, `AutoGatewayExtension` | `self.http.app` (FastAPI), `@http.get/post/put/delete`, `@http.websocket`, `broadcast_to_websockets`, correlation middleware |
| `cliffracer-auth` | `AuthExtension` | JWT auth, `@requires_auth` / `@requires_roles` / `@requires_permissions` |
| `cliffracer-logging` | `LoggingExtension` | structured logging, correlation logging, log-to-NATS |
| `cliffracer-metrics` | `MetricsExtension` | in-process counters over the dispatch hooks |
| `cliffracer-otel` | `OtelExtension` | OpenTelemetry distributed tracing and W3C context propagation |
| `cliffracer-kv` | `KvExtension` | NATS JetStream Key-Value and Object Store integration with bucket TTL |
| `cliffracer-resilience` | `ResilienceExtension` | circuit breaking, sliding-window rate limiting, and resilient RPC |
| `cliffracer-backdoor` | `BackdoorExtension` | the async debug backdoor |
| `cliffracer-cron` | *(none)* | `@cron` handlers. `CronTimer` subclasses core's `Timer`, so core's timer discovery starts them and there is no extension to declare |
| `cliffracer-faststream` | `FastStreamExtension` | FastStream broker hosting, resilient ACK/DLQ routing, and shutdown drain |

Core binds two of its own before any you declare: `CorrelationExtension`, so
every later hook sees a correlation id, and `ValidationExtension`, which
validates every RPC payload against its handler's annotations and backs
`@validated_listener`.

See [extensions.md](extensions.md) for the hook contract and for writing one.

## Authentication

From the `cliffracer-auth` distribution.

### SimpleAuthService

JWT issue and verify, with PBKDF2 password hashing. Usable standalone, without
the extension.

```python
from cliffracer_auth import AuthConfig, SimpleAuthService

config = AuthConfig(
    secret_key="a-secret-key-of-at-least-32-characters",
    token_expiry_hours=24,
)
auth = SimpleAuthService(config)
```

`AuthConfig` fields: `secret_key`, `algorithm`, `token_expiry_hours`,
`enable_auth`, `pbkdf2_iterations`,
`refresh_max_lifetime_hours`.

**Methods:**
- `create_user(username, email, password) -> AuthUser`
- `authenticate(username, password) -> str` (JWT token)
- `validate_token(token) -> AuthContext`
- `refresh_token(token) -> str`
- `revoke_token(token)`
- `add_role(username, role)`
- `add_permission(role, permission)`
- `hash_password(password)` / `verify_password(password, encoded)`

The user store is a dict in the service process, so it lives as long as the
process. It suits tests and small deployments.

### AuthUser

A dataclass.

```python
@dataclass
class AuthUser:
    user_id: str
    username: str
    email: str
    roles: set[str] = field(default_factory=set)
    permissions: set[str] = field(default_factory=set)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    is_active: bool = True
```

Note `user_id`, not `id`, and `set[str]`, not `list`. Password hashes live in
the service's store, not on the user object handed to a handler.

## Decorators

### RPC Decorators

```python
from pydantic import BaseModel

from cliffracer import rpc


class SignupRequest(BaseModel):
    email: str
    age: int


class Signup(BaseModel):
    email: str
    age: int


@rpc
async def my_method(self, param: str) -> dict[str, str]:
    """Basic RPC method"""
    return {"result": param}


@rpc
async def validated_method(self, request: SignupRequest) -> Signup:
    """The annotated parameter is the schema; the handler receives the model"""
    return Signup(email=request.email, age=request.age)
```

### Event Decorators

```python
@listener("user.created", fanout=True)
async def on_user_created(self, subject: str, **data):
    """Subscribe to specific event"""
    pass

@listener("order.*", fanout=True)
async def on_any_order_event(self, subject: str, **data):
    """Subscribe to pattern"""
    pass
```

### Timer Decorators

```python
@timer(interval=60)  # Every 60 seconds
async def periodic_task(self):
    """Run periodically"""
    pass
```

### HTTP Decorators

Available on an `HttpExtension` instance (e.g. `http = HttpExtension(port=8080)`):

```python
@http.get("/users/{user_id}")
async def get_user(self, user_id: str):
    return {"user_id": user_id}

@http.post("/users", status_code=201)
async def create_user(self, user: UserModel):
    return {"id": "123"}

@http.put("/users/{user_id}")
async def update_user(self, user_id: str, user: UserModel):
    return {"status": "updated"}

@http.delete("/users/{user_id}", status_code=204)
async def delete_user(self, user_id: str):
    pass  # No content for 204
```

## Correlation Tracking

### CorrelationContext

Track requests across services.

```python
from cliffracer import (
    get_correlation_id,
    set_correlation_id,
    create_correlation_id,
    with_correlation_id
)

# Get current ID
correlation_id = get_correlation_id()

# Set ID
set_correlation_id("custom-id-123")

# Create new ID
new_id = create_correlation_id()

# Decorator
@with_correlation_id
async def process_request(self):
    # Correlation ID is guaranteed to exist for the duration of this call
    await self.do_work()
```

## Error Handling

### Exception Hierarchy

```python
from cliffracer.core.exceptions import (
    CliffracerError,          # Base exception
    ServiceError,             # Service-level errors
    ConnectionError,          # NATS connection issues
    ConfigurationError,       # Config problems
    ValidationError,          # Input validation
    AuthenticationError,      # Auth failures
    AuthorizationError,       # Permission denied
    RPCError,                 # RPC call failures
)
```

Handle these with ordinary `try`/`except` in your handlers. An exception that
reaches the container is returned to the caller as an error reply and counted
by `MetricsExtension` if it is installed.

## Metrics

`PerformanceMetrics`, `BatchProcessor` and `OptimizedNATSConnection` are in the
`cliffracer-metrics` distribution and import from `cliffracer_metrics`.

`MetricsExtension` counts over the dispatch hooks, so it needs no call in your
handler.

```python
from cliffracer import CliffracerService
from cliffracer_metrics import MetricsExtension, PerformanceMetrics

class MyService(CliffracerService):
    metrics = MetricsExtension()
```

It counts calls, errors and refusals per entrypoint kind and contributes them
to `GET /health` and `GET /info` under its attribute name. A refusal
(`RejectMessage`) is counted as `rejected`, not as an error: a handler
declining a message on purpose is not a fault, and counting it as one made the
error rate unreadable.

Export them from your own service: scrape `/health` and `/info`, or read
`self.metrics` in a handler or a timer and write them to Prometheus, statsd or
a log line.

## Logging

From the `cliffracer-logging` distribution. Import it from
`cliffracer_logging`.

### Service Logger

```python
from cliffracer_logging import get_service_logger

logger = get_service_logger("my_service", user_id="123")

# Logs include the correlation ID automatically
logger.info("Processing request")
```

`get_service_logger(service_name, **context)` returns a `ContextualLogger`;
keyword arguments become permanent context on every line it writes.

### LoggingConfig

`LoggingConfig` is configured through one classmethod, not constructed:

```python
from cliffracer_logging import LoggingConfig

LoggingConfig.configure(
    "my_service",          # service_name, positional and required
    log_level="INFO",
    log_dir=None,          # defaults to $CLIFFRACER_LOG_DIR, else ./logs
    structured=True,       # JSON rather than text
    enable_console=True,
    enable_file=True,
    rotation="10 MB",
    retention="1 week",
    compression="gz",
)
```

## Utilities

### Input Validation

`cliffracer.core.validation` is an internal helper module. It contains
`validate_batch_size`, `validate_password`, `validate_string_length`,
`validate_timeout` and `validate_username` for the library's own use, and it is
reached by that full path.

Validate your handler arguments by annotating them, with Pydantic models where
a parameter is structured, and your event payloads with `@validated_listener`.
That is the supported path, and the one the framework enforces at the
boundary.

### Environment Helpers

Settings are fields on `ServiceConfig`, set in code or in a
`cliffracer run --config` YAML file. An application reads its own environment
with `os.environ`. Extensions read their own prefixed variables — see the table
in the README.
