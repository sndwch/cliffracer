# Cliffracer

Cliffracer is a strongly opinionated, async-native Python framework for building typed NATS services, inspired by Nameko, Lightbus, NestJS, Moleculer, and Zero (`Ananto30/zero`). 

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Lint: ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://docs.astral.sh/ruff/)

## Core

Core gives you RPC, events, timers and a health endpoint. HTTP, WebSockets, authentication, metrics, logging, cron and a debug console are separate distributions you install when you want them.

- **RPC.** `@rpc` on a method exposes it at `{service}.rpc.{method}`. Call it
  with `RpcProxy` or `call_rpc`.
- **Events.** `@listener("user.created", fanout=True)` subscribes; `publish_event` sends.
  Wildcards work.
- **Timers.** `@timer(interval=60)` runs a method on a schedule.
- **JetStream.** Durable consumers, configured through `ServiceConfig`.
- **Binary Serialization.** Configurable payload serialization supporting JSON and
  MsgPack formats via `ServiceConfig.serialization_format`, with automatic content
  negotiation over NATS headers.
- **Health.** `GET /health` and `GET /info` on a stdlib listener, no web
  framework needed.
- **Correlation IDs.** Propagated through NATS messages and readable in any
  handler with `CorrelationContext.get()`.
- **Reconnection.** The client retries forever by default; a connection closed
  for good ends the process so a supervisor restarts it.

Handlers are discovered from their decorators when the service starts.

## Extensions

Each is its own distribution. Declaring one on the class is what loads it.

| distribution | attribute | gives you |
|---|---|---|
| `cliffracer-http` | `HttpExtension`, `AutoGatewayExtension` | FastAPI routes, WebSockets, dynamic RPC Auto-Gateway, and `/health` on the same port |
| `cliffracer-auth` | `AuthExtension` | JWT issue and verify, roles, permissions |
| `cliffracer-metrics` | `MetricsExtension` | call, error and refusal counts over the dispatch hooks |
| `cliffracer-logging` | `LoggingExtension` | structured logging, optionally to a NATS subject |
| `cliffracer-otel` | `OtelExtension` | OpenTelemetry distributed tracing and W3C context propagation |
| `cliffracer-kv` | `KvExtension` | NATS JetStream Key-Value and Object Store integration with bucket TTL |
| `cliffracer-resilience` | `ResilienceExtension` | circuit breaking, sliding-window rate limiting, and resilient RPC |
| `cliffracer-backdoor` | `BackdoorExtension` | a live async Python console, off unless you enable it |
| `cliffracer-cron` | *(none)* | `@cron` handlers, started by core's timer discovery |
| `cliffracer-faststream` | `FastStreamExtension` | FastStream broker hosting, resilient ACK/DLQ routing, and shutdown drain |


## Install

Install Cliffracer and desired extensions from PyPI:

```bash
pip install cliffracer cliffracer-http
```

Releases are cut by pushing a version tag such as `v1.0.0`, which triggers the
release workflow.

### Working on cliffracer itself

```bash
git clone https://github.com/sndwch/cliffracer.git
cd cliffracer
uv sync --all-packages --extra dev
```

### A broker to develop against

```bash
docker run -d --name nats-server -p 4222:4222 -p 8222:8222 \
  nats:alpine -js -m 8222
```

[QUICKSTART.md](QUICKSTART.md) takes you from here to a running service.

## A service

```python
from cliffracer import CliffracerService, ServiceConfig, listener, rpc

class UserService(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="user_service"))

    @rpc
    async def get_user(self, user_id: str) -> dict[str, str]:
        return {"user_id": user_id, "name": "Ada"}

    @listener("user.created", fanout=True)
    async def on_user_created(self, subject: str, user_id: str = ""):
        print(f"user created: {user_id}")

if __name__ == "__main__":
    UserService().run()
```

## HTTP and WebSockets

Install `cliffracer-http` and declare the extension. Routes hang off the
attribute, so the decorator says which extension serves them.

```python
from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer_http import HttpExtension

class NotificationService(CliffracerService):
    http = HttpExtension(port=8080)

    def __init__(self):
        super().__init__(ServiceConfig(name="notification_service"))

    @http.get("/users/{user_id}")
    async def get_user(self, user_id: str) -> dict:
        return {"user_id": user_id}

    @http.websocket("/ws/notifications")
    async def notifications(self, websocket):
        await websocket.accept()
        while True:
            await websocket.receive_text()

    @listener("user.activity", fanout=True)
    async def broadcast_activity(self, subject: str, activity: str = ""):
        await self.http.broadcast_to_websockets({"activity": activity})
```

The websocket handler is bound before the framework calls it, so it takes
`self` and the socket. `broadcast_to_websockets` drops sockets that fail to
send, so one dead client does not stop the fan-out.

The extension serves `/health` and `/info` on its app as well, and the core
listener does not bind. The service has one port — the extension's, 8080 in the
example above — and that is where probes go. `ServiceConfig.health_port` applies
to services that do not declare this extension.

## Health

`GET /health` returns `health_check()`, and encodes the answer in the status
code as well: 200 when `status` is `healthy`, 503 otherwise. That holds on both
implementations — the core listener and the `HttpExtension` route
(`extension.py:112`) evaluate the same expression — so `curl -f` works without
parsing the body, whichever one is serving.

`status` is the field to read on either path, and it has five values:

| `status` | means |
|---|---|
| `"healthy"` | running, connected to NATS, and every declared dependency answered |
| `"unhealthy"` | running and connected, but a declared dependency could not complete a round trip |
| `"connecting"` | running, but NATS broker connection dial is in progress |
| `"disconnected"` | running, but the NATS connection is closed and will not come back on its own |
| `"stopped"` | before `start()`, or after `stop()` |

`"stopped"`, `"connecting"`, and `"disconnected"` outrank `"unhealthy"`: they
are more specific diagnoses, and reporting either as `"unhealthy"` would lose
the reason. Read that vocabulary rather than `"ok"`, which another framework's
convention might lead you to expect.

### Dependencies

A service is often only as healthy as the things it talks to. Declaring a
dependency makes `/health` say so.

**The name is a label and the probe body is the check.** Cliffracer takes the
coroutine you decorate, runs it with a timeout, and records what happened under
the name you gave it. It does not know what "postgres" or "s3" means, and it
never goes and looks at anything itself. Declaring a dependency called `foo`
means exactly this: run this function on every `/health` call, and report the
result under "foo".

#### Writing a probe

A probe is an `async` method taking no arguments beyond `self`, decorated with
`@dependency`. **It does one real round trip to the thing it names, and raises
if that fails.** That round trip is the entire check, and it is the part
cliffracer cannot do for you.

- **Raise on failure**, and let the real exception out — its message is copied
  into the report. Returning normally means healthy.
- **The return value is ignored.** Don't build one.
- **It runs on every `/health` call**, so keep it to one cheap operation. This
  is a liveness check, not a test suite.
- **Give it a timeout shorter than your caller's.** An orchestrator with a
  5-second probe deadline needs an answer well inside that.

```python
from cliffracer import dependency

class Api(CliffracerService):
    @dependency("postgres", timeout=1.0, database="jorbo")
    async def _check_db(self):
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT 1")

    @dependency("s3", timeout=2.0, bucket="uploads")
    async def _check_s3(self):
        # head_bucket is the cheapest call that still proves credentials,
        # network and permissions all work against the bucket we use.
        await self.s3.head_bucket(Bucket=self.bucket)

    @dependency("billing", timeout=2.0, url="https://billing.internal/health")
    async def _check_billing(self):
        response = await self.http_client.get("https://billing.internal/health")
        response.raise_for_status()
```

Each one reaches the dependency and comes back. That is what makes the answer
worth anything.

**The anti-pattern**, because it is the easy thing to write:

```python
class Api(CliffracerService):
    @dependency("postgres")
    async def _check_db(self):
        assert self.pool is not None      # WRONG: the object still exists
        assert self._db_connected         # WRONG: set at startup, never since
```

Both check something this process decided earlier. A connection pool object
still exists after the database stops answering, and a flag set when the
service started says what was true then. **A probe that does no round trip
reports healthy while the thing it names is down** — the framework cannot tell
the difference and will not warn you.

**One probe per name.** Names are unique within a service: each is a key in the
report, so two probes cannot share one. Declaring the same name on two methods
raises at construction, naming both. A subclass may declare a base class's name
against its own probe — that is an override, and the subclass wins.

**Declare one probe per external thing the service needs** — one for postgres,
one for S3 — and never on an RPC method. RPC methods stay `@rpc` and fail on
their own. The runner calls a probe with no arguments on every `/health`, so an
RPC with parameters decorated as a dependency raises `TypeError: get_user()
missing 1 required positional argument: 'user_id'` and reports as that
dependency failing.

Use `service.add_dependency(name, probe, timeout=...)` when the probe is only
knowable at runtime; `probe` is any callable returning an awaitable, and the
same rules apply. Calling it with a name that already exists **replaces** that
probe, which is how you swap one at runtime.

#### What cliffracer does with the results

`/health` reports `"status": "healthy"` only when every probe returned without
raising, inside its own timeout. If any probe raises or times out, `"status"` is
`"unhealthy"`, and the names of the ones that failed are listed under
`unhealthy_dependencies`:

```json
{
  "status": "unhealthy",
  "unhealthy_dependencies": ["postgres"],
  "dependencies": {
    "postgres": {"database": "jorbo", "ok": false,
                 "error": "timed out after 1.0s", "latency_ms": 1000.8},
    "s3":       {"bucket": "uploads", "ok": true,
                 "error": null, "latency_ms": 25.3}
  }
}
```

The keyword arguments beyond `timeout` — `database="jorbo"`,
`bucket="uploads"` — are copied verbatim into that dependency's result and
change nothing else. They are there so an operator reading a failure knows
which postgres could not be reached.

**Probes run only when something asks for `/health`.** There is no timer and no
cache: `health_check()` is called from the core listener and from the
`HttpExtension` route, and nowhere else, and it runs every declared probe once
per call, concurrently. So the cost is your poller's interval times the number
of probes — a 30-second container healthcheck with three probes is three round
trips every 30 seconds, per service. A thousand services declaring the same
database is a thousand pollers' worth of traffic, and each one probes its own
path to that database on purpose: the answer to "can *this* service reach it"
is what `/health` is for, and one service's answer does not transfer to
another's.

Two more things the framework does, both in `core/dependencies.py`:

- **Probes run concurrently**, each bounded by its own `timeout`
  (`asyncio.gather`, `dependencies.py:133`). Five 2-second checks cost about
  two seconds, not ten.
- **A probe that raises fails that dependency, not the endpoint.** The
  exception is caught and recorded against the name, so `/health` still answers
  when something is wrong. A timeout is recorded as `timed out after Ns`,
  distinct from a refusal, because a slow dependency and an absent one need
  different fixes.

## Auto-Gateway for HTTP Ingress

`AutoGatewayExtension` in `cliffracer-http` mounts dynamic FastAPI HTTP routes mapped
directly to downstream RPC methods discovered via introspection or service declarations.
It infers HTTP verbs from method name prefixes, extracts path, query, and JSON body
parameters, forwards calls via NATS RPC, translates errors to HTTP status codes, and
exposes interactive OpenAPI documentation:

```python
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer_http import AutoGatewayExtension, HttpExtension


class UserModel(BaseModel):
    id: str
    name: str
    email: str


class CreateUserPayload(BaseModel):
    name: str
    email: str


class UserService(CliffracerService):
    name = "users"
    http = HttpExtension(port=8080)
    gateway = AutoGatewayExtension(prefix="/api/v1")

    @rpc
    async def get_user(self, user_id: str) -> UserModel:
        """Fetch user by id."""
        return UserModel(id=user_id, name="Alice", email="alice@example.com")

    @rpc
    async def create_user(self, payload: CreateUserPayload) -> UserModel:
        """Create new user."""
        return UserModel(id="usr_1", name=payload.name, email=payload.email)

    @rpc
    async def delete_user(self, user_id: str) -> bool:
        """Delete user by id."""
        return True
```

HTTP verbs are automatically inferred from method prefixes:
- `get_user` -> `GET /api/v1/users/get_user?user_id=...` (`get_`, `list_`, `fetch_`, `find_` -> `GET`)
- `create_user` -> `POST /api/v1/users/create_user` (`create_`, `add_`, `post_`, `insert_` -> `POST`, request body JSON)
- `delete_user` -> `DELETE /api/v1/users/delete_user?user_id=...` (`delete_`, `remove_`, `drop_` -> `DELETE`)

Interactive Swagger UI documentation is available at `http://localhost:8080/docs`.

## Distributed Tracing with OpenTelemetry

Install `cliffracer-otel` to enable W3C distributed tracing across inbound handlers
and outbound RPC calls. Declare `OtelExtension` on your service class:

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer_otel import OtelExtension

class OrderService(CliffracerService):
    otel = OtelExtension()

    def __init__(self):
        super().__init__(ServiceConfig(name="order_service"))
```

The extension extracts `traceparent` headers from incoming NATS messages in `worker_setup`,
creates server spans linked to `cliffracer.subject` and `cliffracer.correlation_id`, injects
W3C headers into outbound calls in `before_call`, and records exceptions on failures.

## Key-Value and Object Store

Install `cliffracer-kv` for NATS JetStream Key-Value and Object Store capabilities.
Declare `KvExtension` with bucket and object store definitions:

```python
from pydantic import BaseModel
from cliffracer import CliffracerService, ServiceConfig
from cliffracer_kv import BucketConfig, KvExtension

class Profile(BaseModel):
    name: str
    email: str

class AccountService(CliffracerService):
    kv = KvExtension(
        buckets=[
            BucketConfig(name="profiles", ttl=3600),
            "sessions",
        ],
        bucket_ttls={"sessions": 86400},
        object_stores=["documents"],
    )

    def __init__(self):
        super().__init__(ServiceConfig(name="account_service", jetstream_enabled=True))

    async def save_profile(self, user_id: str, profile: Profile) -> int:
        return await self.kv.put("profiles", user_id, profile)

    async def get_profile(self, user_id: str) -> Profile | None:
        return await self.kv.get("profiles", user_id, as_type=Profile)
```

The extension creates declared buckets at startup, supports bucket-level TTL configurations
passed directly to JetStream bucket creation, handles model serialization, and exposes
object storage via `put_object` and `get_object`.

## Circuit Breaking and Rate Limiting

Install `cliffracer-resilience` to protect services against cascading dependency failures
and excessive request rates:

```python
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer_resilience import (
    CircuitBreakerConfig,
    ResilienceExtension,
    ResilientRpcProxy,
    rate_limit,
)

class InventoryService(CliffracerService):
    resilience = ResilienceExtension()
    billing = ResilientRpcProxy(
        "billing_service",
        config=CircuitBreakerConfig(failure_threshold=5, recovery_timeout=30.0),
    )

    def __init__(self):
        super().__init__(ServiceConfig(name="inventory_service"))

    @rpc
    @rate_limit(calls=100, window=60)
    async def reserve_stock(self, item_id: str, count: int) -> dict[str, str]:
        charge = await self.billing.charge(item_id=item_id, count=count)
        return {"reserved": item_id, "status": "confirmed"}
```

`ResilientRpcProxy` fails fast locally without sending wire traffic when downstream
services fail repeatedly. `@rate_limit` enforces call rate thresholds using in-memory
or distributed NATS KV sliding windows, rejecting excess requests over the wire.

## Binary Serialization with MsgPack

Set `serialization_format="msgpack"` in `ServiceConfig` to enable MessagePack binary
serialization for high-throughput RPC and event dispatches:

```python
from cliffracer import CliffracerService, ServiceConfig, rpc

class FastService(CliffracerService):
    def __init__(self):
        super().__init__(
            ServiceConfig(
                name="fast_service",
                serialization_format="msgpack",
            )
        )

    @rpc
    async def compute(self, values: list[int]) -> dict[str, int]:
        return {"total": sum(values)}
```

Services send and inspect the `content-type` header (`application/msgpack` vs
`application/json`), automatically decoding binary payloads and encoding replies.

## Idempotent Publishing

Decorate methods with `@idempotent` to ensure outgoing messages publish with deterministic `Nats-Msg-Id` headers:

```python
from cliffracer import CliffracerService, ServiceConfig, idempotent

class OrderService(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="order_service", jetstream_enabled=True))

    @idempotent(key="order_id")
    async def process_order(self, order_id: str, amount: float):
        """Publish with deterministic Nats-Msg-Id for JetStream deduplication."""
        await self.publish_event("order.processed", order_id=order_id, amount=amount)
```

`@idempotent` extracts the specified key from method arguments, or hashes the payload with SHA-256 when using `hash_payload=True` or bare `@idempotent`.

> **Note**: `@idempotent` relies entirely on JetStream's native message deduplication window via the `Nats-Msg-Id` header. It operates without requiring `cliffracer-kv` or an external cache.

## Typed clients

A service that annotates its handlers can hand out a client. The annotations
are the contract, the service publishes them on `{service}.describe`, and
`cliffracer-generate-client` turns that into a file you check in.

```python
from cliffracer import CliffracerService, ServiceConfig, rpc
from pydantic import BaseModel


class Line(BaseModel):
    sku: str
    qty: int = 1


class Receipt(BaseModel):
    order_id: str
    total_qty: int


class Warehouse(CliffracerService):
    @rpc
    async def create(self, lines: list[Line], note: str = "") -> Receipt:
        """Create an order and return its receipt."""
        return Receipt(order_id="o-1", total_qty=sum(line.qty for line in lines))
```

Generate from the class, or from a service that is already running:

```text
cliffracer-generate-client --class myapp.warehouse:Warehouse \
    --service warehouse --version 1.0.0 --out warehouse_client.py

cliffracer-generate-client --service warehouse --nats-url nats://localhost:4222 \
    --out warehouse_client.py
```

Both produce the same bytes, which
`tests/integration/test_typed_client_end_to_end.py` asserts. A service behind
`AuthExtension` refuses an unauthenticated describe like any other message, so
give the live form credentials:

```text
cliffracer-generate-client --service warehouse \
    --header authorization="bearer $TOKEN" --out warehouse_client.py
```

The generated file imports the models rather than copying them, so a change to
`Line` reaches the client the way it reaches everything else. Calling it from
another service -- the import is the file `--out` wrote, so this block is shown
rather than run:

```text
from warehouse_client import WarehouseClient


class Consumer(CliffracerService):
    async def on_startup(self):
        self.warehouse = WarehouseClient(self.nc, service="warehouse")

    @rpc
    async def restock(self, sku: str) -> str:
        receipt = await self.warehouse.create([Line(sku=sku, qty=10)])
        return receipt.order_id
```

**The client checks itself against the service.** On its first call it reads
the live description and compares a hash per method with the ones it was
generated from. A method whose signature moved, or that is gone, raises
`ClientOutOfDate` naming it -- before the call goes out, rather than as a field
that will not deserialise. A method the service ADDED is not drift: the client
can still make every call it knows how to make.

Everything else that can go wrong has its own exception, so a caller can tell
them apart without reading message text: `RpcValidationError` carries
pydantic's own `details`, `RpcRefused` carries the reason an extension gave,
`RpcUnknownMethod` means the service is running and has no such method,
`RpcNoResponders` means nothing is subscribed at all, and `RpcTimeout` means
nobody answered in time.

The command's exit codes are for scripts:

| code | meaning |
|---|---|
| 0 | a client was written |
| 2 | the broker answered and no such service did |
| 3 | no broker at that address |
| 4 | the service cannot be described, or its description cannot be emitted |
| 5 | the class named by `--class` could not be imported |

### A permanently closed connection stops the service

`max_reconnect_attempts=-1` by default, so the client retries indefinitely and a
broker reboot is a non-event. If the connection closes permanently, the service
logs at ERROR and triggers graceful shutdown (`await self.stop()`). Set
`exit_on_closed=False` to keep the service running and observe `"disconnected"`
on the health endpoint.

## Running services

```bash
cliffracer run myapp.services                  # every service in the module
cliffracer run myapp.services:OrderService
```

## Configuration

Core settings are fields on `ServiceConfig`, set in code or in a
`cliffracer run --config` YAML file. `ServiceConfig` forbids unknown keys, so a
name it does not recognise raises `ValidationError` at construction rather than
being ignored.

```python
from cliffracer import CliffracerService, ServiceConfig

config = ServiceConfig(
    name="my_service",
    nats_url="nats://localhost:4222",
    request_timeout=10.0,
)
service = CliffracerService(config)
```

`docs/api-reference.md` has the full field table.

### Environment variables

Each installed extension owns a prefix and reads its own:

| variable | read by | effect |
|---|---|---|
| `CLIFFRACER_HTTP_HOST`, `CLIFFRACER_HTTP_PORT` | `cliffracer-http` | where the HTTP app binds |
| `CLIFFRACER_BACKDOOR_ENABLED` | `cliffracer-backdoor` | turns the console on; it is off by default |
| `CLIFFRACER_BACKDOOR_PORT`, `CLIFFRACER_BACKDOOR_PASSWORD` | `cliffracer-backdoor` | console port and password |
| `CLIFFRACER_LOG_DIR` | `cliffracer-logging` | directory for file logging, default `./logs` |

Your own settings are yours to read: pull them from `os.environ` or your own
`pydantic_settings.BaseSettings`, and pass the values to whatever needs them.

### NATS authentication

`ServiceConfig` takes `nats_user` and `nats_password`, `nats_token`, or
`nats_credentials_file`. All are optional; unset, the service connects
unauthenticated.

```python
config = ServiceConfig(
    name="my_service",
    nats_url="nats://broker:4222",
    nats_user="svc",
    nats_password="s3cret",
)
```

Prefer these fields to a password embedded in `nats_url`. The connect log line
is redacted either way — an embedded password logs as `nats://***@broker:4222` —
but a URL credential still reaches argv and error messages. The fields do not.

They work in a `--config` YAML file with no extra wiring, since
`cliffracer.cli.config` validates every key against `ServiceConfig.model_fields`:

```yaml
# deploy.yaml
my_service:
  nats_url: nats://broker:4222
  nats_user: svc
  nats_password: s3cret
```

That is the path to use for authenticated deployments. Passing `--nats-url`
with an embedded password puts the credential in argv, where anything that can
read the process list can see it.

`nats_credentials_file` takes a path to a NATS `.creds` file. A path is not a
secret, so it can live in a config file or in version control.

## Production

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . /app
RUN pip install uv && uv sync --no-dev
CMD ["python", "-m", "your_service"]
```

### Kubernetes

A cliffracer service is an ordinary Python process with one port to probe.
Point a readiness probe at `GET /health` and let the status code decide: 200
healthy, 503 otherwise, on whichever listener is serving. Read `status` from
the body when you want to know *which* dependency failed.

### JetStream tuning

```python
config = ServiceConfig(
    name="production_service",
    # How much work the broker hands this replica at once. Raise it to use a
    # bigger machine; lower it to spread work across more replicas.
    jetstream_max_ack_pending=256,
    # How greedily one replica fills that budget per fetch. Deliberately small:
    # a large batch parked in one replica is the imbalance pull consumers exist
    # to avoid.
    jetstream_pull_batch=8,
    # Long enough that a slow handler is not redelivered under itself.
    jetstream_ack_wait=60.0,
)
```

`jetstream_max_ack_pending` and `jetstream_ack_wait` are write-once per durable
consumer: change them and restart, and the service asks for one thing while the
server does another. [docs/api-reference.md](docs/api-reference.md) explains how
to apply a change.

## Layout

```
src/cliffracer/
  core/
    service.py                the service class
    container.py              connection lifecycle, dispatch, exit handling
    decorators.py             @rpc, @listener, @timer, @broadcast
    dependencies.py           the @dependency probe machinery
    health_listener.py        /health and /info without a web framework
    jetstream.py              durable consumer setup
    service_config.py         the ServiceConfig model
    validation.py             handler signature and payload validation
  cli/                        the `cliffracer run` command
  runners/orchestrator.py     running several services in one process
  rpc_proxy.py                RpcProxy
packages/                     the extension distributions
```

## Tests

```bash
uv run pytest                                   # everything; NATS-marked tests need a broker
uv run pytest tests/unit                        # no broker required
uv run pytest tests/transport                   # no broker required
uv run pytest tests/repo                        # no broker required
uv run pytest tests/integration                 # needs NATS_URL
uv run pytest --cov=src/cliffracer --cov-report=html
```

`tests/integration/test_examples_run.py` launches every example under
`examples/` against a real broker and sends it SIGINT, so the examples are
checked as documentation rather than assumed to work.

## Documentation

- [QUICKSTART.md](QUICKSTART.md) — install to running service
- [docs/philosophy.md](docs/philosophy.md) — landscape, design philosophy, and why NATS
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system design
- [docs/api-reference.md](docs/api-reference.md) — classes, methods, config fields
- [docs/http-guide.md](docs/http-guide.md) — REST APIs
- [docs/websocket-guide.md](docs/websocket-guide.md) — real-time communication
- [docs/extensions.md](docs/extensions.md) — writing and using extensions
- [docs/correlation.md](docs/correlation.md) — distributed correlation ID tracking
- [docs/performance.md](docs/performance.md) — batch processing and connection pools
- [docs/decisions.md](docs/decisions.md) — the constraints behind the design
- [docs/debugging/backdoor.md](docs/debugging/backdoor.md) — the live console
- [examples/](examples/) — runnable services

## Contributing

Branch, commit, push, open a pull request.

## License

MIT. See [LICENSE](LICENSE).
