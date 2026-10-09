# Cliffracer

Cliffracer is a strongly opinionated, async-native Python framework for building typed NATS services, inspired by Nameko, Lightbus, NestJS, Moleculer, and Zero (`Ananto30/zero`). 

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Lint: ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://docs.astral.sh/ruff/)

## Core

Core gives you RPC, events, timers and a health endpoint. Authentication, metrics, logging, cron and more are separate distributions you install when you want them.

- **RPC.** `@rpc` on a method exposes it at `{service}.rpc.{method}`. Call it
  with `RpcProxy` or `call_rpc`.
- **Events.** `@listener("user.created", fanout=True)` subscribes; `publish_event` sends.
  Wildcards work.
- **Timers.** `@timer(interval=60)` runs a method on a schedule.
- **JetStream.** Durable consumers, configured through `ServiceConfig`.
- **Binary Serialization.** Configurable payload serialization supporting JSON and
  MsgPack formats via `ServiceConfig.serialization_format`, with automatic content
  negotiation over NATS headers.
- **Health.** `GET /live`, `GET /ready`, `GET /health` and `GET /info` on a
  stdlib listener, no web framework needed.
- **Correlation IDs.** Propagated through NATS messages and readable in any
  handler with `CorrelationContext.get()`.
- **Reconnection.** The client retries forever by default; a connection closed
  for good ends the process so a supervisor restarts it.

Handlers are discovered from their decorators when the service starts.

## Extensions

Each is its own distribution. Declaring one on the class is what loads it.

| distribution | attribute | gives you |
|---|---|---|
| `cliffracer-auth` | `AuthExtension` | JWT issue and verify, roles, permissions |
| `cliffracer-metrics` | `MetricsExtension`, `PoolExtension` | call, error and refusal counts over the dispatch hooks; a pool of broker connections beside the service's own |
| `cliffracer-logging` | `LoggingExtension` | structured logging, optionally to a NATS subject |
| `cliffracer-otel` | `OtelExtension` | OpenTelemetry distributed tracing and W3C context propagation |
| `cliffracer-kv` | `KvExtension` | NATS JetStream Key-Value and Object Store integration with bucket TTL |
| `cliffracer-resilience` | `ResilienceExtension` | circuit breaking, sliding-window rate limiting, and resilient RPC |
| `cliffracer-cron` | *(none)* | `@cron` handlers, started by core's timer discovery |
| `cliffracer-dlq` | *(none)* | `cliffracer-dlq`, a read-only command that lists, shows and counts dead letters |
| `cliffracer-cyanide` | `CyanideExtension` | error injection and mundane failure modes for testing |


## Install

Install Cliffracer and desired extensions from PyPI:

```bash
pip install cliffracer cliffracer-auth
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

## Health

`GET /health` returns `health_check()`, and encodes the answer in the status
code as well: 200 when `status` is `healthy`, 503 otherwise, so `curl -f` works
without parsing the body. `GET /ready` is the same answer under the name a
readiness probe expects. `GET /live` answers only whether the process is
running (200, or 503 once stopped) and never consults the broker or a
dependency; use it for a liveness probe, and see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#health) for why.

`status` is the field to read, and it has five values:

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

A stopped service runs no probe: its `/health` answers `"stopped"` at once, with no
`dependencies` block and no `unhealthy_dependencies`, and its downstreams are not
called. `"connecting"` and `"disconnected"` still run the probes and report them.

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

The keywords after `timeout` (`database`, `bucket` and `url` above) are published on
`/health` exactly as given, and `/health` can be read without authentication: name a host
or a database, never a password or a connection string that holds one.

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
cache: `health_check()` is called from the core listener and nowhere else, and it runs every declared probe once
per call, concurrently. So the cost is your poller's interval times the number
of probes — a 30-second container healthcheck with three probes is three round
trips every 30 seconds, per service. A thousand services declaring the same
database is a thousand pollers' worth of traffic, and each one probes its own
path to that database on purpose: the answer to "can *this* service reach it"
is what `/health` is for, and one service's answer does not transfer to
another's.

Two more things the framework does, both in `core/dependencies.py`:

- **Probes run concurrently**, each bounded by its own `timeout`
  (`asyncio.gather`). Five 2-second checks cost about two seconds, not ten.
  The call returns when the timeout passes: a probe slow to honour its
  cancellation is left to finish and does not extend `/health`. A probe that
  blocks the event loop (a synchronous driver call inside `async def`) cannot
  be interrupted, so `/health` waits for it, but it is reported as failed
  (`exceeded its Ns timeout`), never ok.
- **A probe that raises fails that dependency, not the endpoint.** The
  exception is caught and recorded against the name, so `/health` still answers
  when something is wrong. A timeout is recorded as `timed out after Ns`,
  distinct from a refusal, because a slow dependency and an absent one need
  different fixes; a `TimeoutError` the probe itself raises (a driver's own
  timeout) is the probe's failure, not this one.

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
creates a span per dispatch (`SERVER` for an RPC or async RPC, `CONSUMER` for an event,
`INTERNAL` for a timer, none for `describe`) linked to `cliffracer.subject` and
`cliffracer.correlation_id`, injects W3C headers into outbound calls in `before_call`, and
records exceptions on failures.

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
`application/json`), automatically decoding binary payloads and encoding replies. Both formats
carry the same values: integer map keys arrive as strings and `bytes` as `str` either way.

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

`@idempotent` extracts the specified key from method arguments, or hashes the payload with SHA-256 when using `hash_payload=True` or bare `@idempotent`. A key that does not resolve, or resolves to `None`, raises `IdempotencyKeyError` rather than falling back to the payload hash.

> **Note**: `@idempotent` relies entirely on JetStream's native message deduplication window via the `Nats-Msg-Id` header. It operates without requiring `cliffracer-kv` or an external cache.

## Typed clients

[Broker permissions](docs/broker-permissions.md) derive service and client NATS
grants from the declared contracts, with dedicated inbox prefixes and bounded
reply permissions.

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

Both produce the same bytes when `--version` is the version the service's
`ServiceConfig` declares, which
`tests/integration/test_typed_client_end_to_end.py` asserts. A class cannot see
the config it is started with, so the class form records `--version`, or the
`ServiceConfig` default without it; the live form records what the running
service reports and refuses `--version`. A service in a namespace needs
`--namespace` on either form, and the client it writes calls in that namespace
by default. A service behind `AuthExtension` refuses an unauthenticated describe
like any other message, so give the live form credentials:

```text
cliffracer-generate-client --service warehouse \
    --header authorization="bearer $TOKEN" --out warehouse_client.py
```


For a build or CI check, compare the checked-in client without rewriting it:

```text
cliffracer-generate-client --class myapp.warehouse:Warehouse \
    --service warehouse --version 1.0.0 --out warehouse_client.py \
    --check --require-source-under ./src
```

`--check` requires `--out` and works with both class and live-service generation.
It compares the complete UTF-8 output byte for byte, including metadata,
docstrings, formatting and line endings. A match exits zero quietly. A missing
or stale client exits eight and names missing, extra and changed RPCs when the
existing file's signature table and method definitions can be read. Other
content differences and malformed metadata are reported explicitly. The existing
client is read as data, never imported or rewritten; missing output directories
are left absent. Regenerate with the same arguments without `--check`.

`--require-source-under PATH` works with `--class`, for both generation and
checking. The directory must exist. The defining class's source file must resolve
inside it, including through re-exports and symlinks. A wrong checkout or a class
without verifiable source exits nine and leaves the output untouched. This checks
the class definition's location; imported shared models may live elsewhere, and
service imports still execute normally. It verifies the interpreter's selection
without changing `PYTHONPATH` or the editable install.

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
validation `details` (Pydantic's own by default) -- and is the one class raised for a bad argument
whichever end refused it, because the remedy is the same; the message says
which, and that distinction alone is in the text -- `RpcRefused` carries the
reason an extension gave, `RpcUnknownMethod` means the service is running and
has no such method,
`RpcNoResponders` means nothing is subscribed at all, and `RpcTimeout` means
nobody answered in time.

Services accepting sensitive RPC input can set
`ServiceConfig(rpc_validation_errors="redacted")`. Request validation and decode
failures then use fixed diagnostics in broker replies, async logs, and validation
exception chains. The default `"full"` includes rejected values, field locations,
and validator messages; Pydantic's `hide_input_in_errors=True` alone does not hide
these structured details. See the [RPC validation policy](docs/api-reference.md#rpc-validation-diagnostics)
for its scope.

The command's exit codes are for scripts:

| code | meaning |
|---|---|
| 0 | a client was written, or `--check` found an exact match |
| 2 | the broker answered and no such service did |
| 3 | no broker at that address, or a broker that refused this client's credentials or permissions |
| 4 | the service cannot be described, has no rpc handler, or its description cannot be emitted |
| 5 | the class named by `--class` could not be imported, or is not a class |
| 6 | the client could not be written where `--out` asked |
| 7 | the command line is wrong: a missing, unknown or malformed flag, a flag the chosen mode does not use, or a service name, namespace or (when a running service is asked) `$CLIFFRACER_SUBJECT_PREFIX` that cannot be part of a subject |
| 8 | `--check` found a missing or stale generated client |
| 9 | the service class source could not be verified under the required path |
| 10 | `--check` could not read the output file |

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
cliffracer run myapp.services --log-level WARNING
```

`--log-level` sets the level for the whole process: the orchestrator points
loguru at one sink before any service starts, so every service run by that
command logs at that level. That sink prints the frames of a traceback and not
the values in them, so a credential a service was configured with is not
printed. Leave the flag off and the command keeps loguru's default level
but replaces its default sink with one that prints no values either; an
application that embeds the services through `ServiceOrchestrator` keeps
whatever logging it has configured.

## Describing and calling a running service

```bash
cliffracer describe orders                     # what it offers, as a person reads it
cliffracer describe orders --json              # the description as the service sent it
cliffracer call orders.place --json-args '{"item": {"sku": "bolt", "quantity": 3}}' --arg priority=2
cliffracer call orders.place --arg priority=2 --json-args - < order.json
cliffracer call orders.ping --arg word=hi --dry-run
```

Both ask the running service for its description first. `call` then checks the call against it
before sending anything:

- the method is one the service lists;
- every argument is one it takes;
- every argument without a default is given;
- a scalar or literal argument is of that kind.

The service judges the values. `--arg name=value` gives one scalar or literal argument, and
`--json-args` gives them all as one JSON object (`-` reads it from stdin).
The request carries `--timeout` as its `Cliffracer-Timeout-Ms` budget, so the service stops the
handler when the command stops waiting. A `--timeout` of `inf`, or over one day (the most a
service reads as a budget), sends none, and the service applies its own `max_rpc_processing_time`.
A budget given with `--header` is sent as given. The request is the one `cliffracer.calls.prepare`
builds, so it carries the correlation id under `X-Correlation-ID` and `correlation_id`, from a
`--header` in either spelling or a new one.

The result is written to stdout as JSON. An error the service answered is written to stderr as a
JSON object (`code`, `error`, and `details` or `retry_after` when it has them), followed by a line
saying what happened. The exit code says which:

| code | meaning |
|---|---|
| 0 | answered with a result (or described) |
| 2 | the broker answered but no such service did |
| 3 | no broker at that address, or the broker refused this client's credentials or a permission |
| 4 | the service cannot be described, or the method streams its reply |
| 7 | the command line is wrong |
| 11 | the arguments were refused, before sending or by the service |
| 12 | no such method |
| 13 | the service is busy; the error object carries `retry_after` |
| 14 | the service's deadline passed |
| 15 | the service refused the call, as an auth or policy extension does |
| 16 | any other error, or a reply that is not one |

Only a method the description lists can be called: a name cannot become another subject, and
there is no fire-and-forget call and no event publish. `--dry-run` prints the subject, headers and
payload without sending. A call is a real call; nothing tells a method that changes state from one
that does not.

The connection options are the same for both: `--server`, then `$CLIFFRACER_NATS_URL`, then
`$NATS_URL`; `--creds`, `--user`, `--password` and `--token` (or `$NATS_CREDS`, `$NATS_USER`,
`$NATS_PASSWORD`, `$NATS_TOKEN`, where a process list does not show them); `--inbox-prefix` for a
role the broker confines to one; `--namespace`; `--timeout`; and `--header NAME=VALUE`, repeatable,
which a service behind `AuthExtension` needs as `--header authorization="bearer <token>"`.

## Configuration

Core settings are fields on `ServiceConfig`, set in code or in a
`cliffracer run --config` YAML file. `ServiceConfig` forbids unknown keys, so a
name it does not recognise raises pydantic's `ValidationError` at construction rather than
being ignored. In a `--config` file or a flag, a name the config does not have and a value it
refuses (a `restart_delay` below 0, a `nats_user` without a `nats_password`) both end
`cliffracer run` with exit code 2 and a message that names the service, the field and the
reason; the message never repeats the value.

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

These are the variables the library and its packages read:

| variable | read by | effect |
|---|---|---|
| `CLIFFRACER_SUBJECT_PREFIX` | core: `ServiceConfig`, `ServiceClient` and the client generator | the outermost prefix of every subject, stream and durable consumer, used when no `subject_prefix` is given; unset or empty means none |
| `CLIFFRACER_NATS_URL` | the `cliffracer-generate-client` command; `cliffracer describe` and `cliffracer call` | the broker it asks for a service's description when `--nats-url` (`--server` for `describe` and `call`) is not given, before the default `nats://localhost:4222` |
| `LOGURU_LEVEL` | the `cliffracer run` command | loguru's own variable: the level of the stderr sink the command installs in place of loguru's default when `--log-level` is not given (default DEBUG) |
| `CLIFFRACER_LOG_DIR` | `cliffracer-logging` | directory for file logging, default `./logs` (an empty value is the default; a path separator in a service name becomes `_` in its file names) |
| `CLIFFRACER_CYANIDE_<FIELD>` | `cliffracer-cyanide` | one variable for each field of `CyanideConfig` (`ENABLED`, `MODE`, `SLOW_DELAY`, `RAISE_DELAY`, `SLEEP_TIMEOUT_DURATION`, the four `*_WEIGHT`, `SEED`, `INJECTION_RECORD_LIMIT`); fault injection is off unless `CLIFFRACER_CYANIDE_ENABLED` is true |
| `NATS_URL`, `NATS_CREDS`, `NATS_USER`, `NATS_PASSWORD`, `NATS_TOKEN` | `cliffracer-dlq`; `cliffracer describe` and `cliffracer call` | the defaults of their `--server`, `--creds`, `--user`, `--password` and `--token` flags (for `describe` and `call`, `$NATS_URL` after `$CLIFFRACER_NATS_URL`); a password or token belongs here, where a process list does not show it |

The other packages read none. A service's own credentials (`nats_user`, `nats_password`,
`nats_token`) are `ServiceConfig` fields and are not read from the environment.

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

`nats_password` and `nats_token` are held as `SecretStr`, so `repr(config)`, `model_dump_json()` and a
validation error do not print them; `config.nats_password.get_secret_value()` reads one.

Prefer these fields to a password embedded in `nats_url`. The connect log line
is redacted either way — an embedded password logs as `nats://***@broker:4222` —
but a URL credential still reaches argv and error messages. The fields do not.

They work in a `--config` YAML file with no extra wiring, since
`cliffracer.cli.config` validates every key against `ServiceConfig.model_fields`:

```yaml
# deploy.yaml
services:
  my_service:
    nats_url: nats://broker:4222
    nats_user: svc
    nats_password: s3cret
```

The file has a `global:` section, whose settings apply to every service in the run, and a
`services:` section keyed by service name. A key outside those two is refused, and a name under
`services:` that no service in the run has is reported as a warning naming the services that are run.

That is the path to use for authenticated deployments. Passing `--nats-url`
with an embedded password puts the credential in argv, where anything that can
read the process list can see it.

`nats_credentials_file` takes a path to a NATS `.creds` file. A path is not a
secret, so it can live in a config file or in version control.

## Production

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN pip install uv && uv sync --no-dev
CMD ["python", "-m", "your_service"]
```

### Kubernetes

A cliffracer service is an ordinary Python process with one port to probe.
Point a readiness probe at `GET /ready` and let the status code decide: 200
healthy, 503 otherwise, on whichever listener is serving. Point a liveness probe
at `GET /live`, which stays 200 while the process runs whatever the broker is
doing; a liveness probe on `/ready` or `/health` restarts the pod whenever the
broker is down for longer than the probe's failure threshold. Read `status` from the body when you want to know *which*
dependency failed.

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

`jetstream_ack_wait` is not a bound on how long one replica may hold a message.
The heartbeat resets the server's ack timer for as long as the handler has not
returned, which is what keeps a slow handler from being redelivered under
itself — and what lets a WEDGED handler hold the message forever, with
`jetstream_max_deliver` and the dead-letter queue never firing. Set
`max_processing_time` to bound it: a handler that outlives it is cancelled and
the message naked, then dead-lettered like any other failure. It is `None` by
default, so nothing changes until you choose a number.

A cancelled handler is one that had already signed up for redelivery, so
partial work followed by a retry is within its contract; a handler that cannot
tolerate cancellation cannot tolerate redelivery either.

The budget is enforced by cancellation, so it bounds a handler that lets
`asyncio.CancelledError` propagate. A handler that suppresses it (catching
`CancelledError`, or `BaseException`) is outside that bound. If it then returns,
the message is still naked and a WARNING says the cancellation was suppressed,
so the work it went on to do may be delivered again: a handler that is not
idempotent must let the cancellation through. If it keeps running, the message
cannot be redelivered under it and stays in flight with no disposition, and a
WARNING says the handler is still running once it reaches twice
`max_processing_time`.

`jetstream_max_ack_pending`, `jetstream_ack_wait` and `jetstream_max_deliver`
are write-once per durable consumer: change them and restart, and the service
asks for one thing while the server does another.
[docs/api-reference.md](docs/api-reference.md) explains how to apply a change.
For `jetstream_max_deliver` the lower of the two decides when a failing message
is dead-lettered, so a message is not lost when the server stops redelivering
first.
[Dead letters](docs/dead-letters.md) says what a dead-letter record holds and how to read
the dead-letter subject with the `nats` command line.

## Layout

```
src/cliffracer/
  core/
    service.py                the service class
    container.py              connection lifecycle, dispatch, exit handling
    decorators.py             @rpc, @listener, @timer, @broadcast
    dependencies.py           the @dependency probe machinery
    health_listener.py        /live, /ready, /health and /info without a web framework
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
uv run pytest tests/integration                 # needs CLIFFRACER_TEST_NATS_URL
uv run pytest --cov=src/cliffracer --cov-report=html
```

`tests/integration/test_examples_run.py` launches every example under
`examples/` against a real broker and sends it SIGINT, so the examples are
checked as documentation rather than assumed to work. A long-running example
prints a line beginning `EXAMPLE READY:` once the thing it demonstrates has
happened (an order created, a timer run, an RPC answered), and the test waits
for that line.

## Documentation

- [QUICKSTART.md](QUICKSTART.md) — install to running service
- [docs/philosophy.md](docs/philosophy.md) — landscape, design philosophy, and why NATS
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system design
- [docs/api-reference.md](docs/api-reference.md) — classes, methods, config fields
- [docs/extensions.md](docs/extensions.md) — writing and using extensions
- [docs/correlation.md](docs/correlation.md) — distributed correlation ID tracking
- [docs/performance.md](docs/performance.md) — batch processing and connection pools
- [docs/decisions.md](docs/decisions.md) — the constraints behind the design
- [docs/upgrading.md](docs/upgrading.md) — what to change in a deployment for each breaking change, with a before and an after
- [docs/virtual-services.md](docs/virtual-services.md) — local template and activation contract, with future placement boundaries
- [docs/service-templates.md](docs/service-templates.md) — registered service factories and typed activation references
- [docs/local-supervisor.md](docs/local-supervisor.md) — bounded local activation, owner lifetimes and cleanup outcomes
- [examples/](examples/) — runnable services

## Contributing

Branch, commit, push, open a pull request.

## License

MIT. See [LICENSE](LICENSE).
