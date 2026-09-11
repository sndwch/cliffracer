# Extensions

An extension is an object declared as a class attribute on a service, bound
per service instance, and run by the container. **`setup`, `start` and
`worker_setup` run in declaration order; `worker_result`, `worker_teardown` and
`stop` run in reverse**, so an extension is torn down inside the ones declared
before it. Every hook is optional.

```python
from cliffracer import CliffracerService
from cliffracer_http import HttpExtension
from cliffracer_metrics import MetricsExtension

class Orders(CliffracerService):
    http = HttpExtension(port=8080)
    metrics = MetricsExtension()
```

The attribute name does three jobs. It is how you reach the extension
(`self.http.app`), how its decorators are spelled (`@http.get("/orders")`), and
the key its contributions appear under in `/health` and `/info`.

## The distributions

| distribution | attribute type | provides |
|---|---|---|
| `cliffracer-http` | `HttpExtension`, `AutoGatewayExtension` | FastAPI app, REST routes, websockets, dynamic RPC Auto-Gateway |
| `cliffracer-auth` | `AuthExtension` | JWT auth, `@requires_auth` / `@requires_roles` / `@requires_permissions` |
| `cliffracer-logging` | `LoggingExtension` | structured logging, correlation logging, log-to-NATS |
| `cliffracer-metrics` | `MetricsExtension` | in-process counters over the dispatch hooks |
| `cliffracer-otel` | `OtelExtension` | OpenTelemetry distributed tracing and W3C context propagation |
| `cliffracer-kv` | `KvExtension` | NATS JetStream Key-Value and Object Store with bucket TTL |
| `cliffracer-resilience` | `ResilienceExtension` | circuit breaking, sliding-window rate limiting, resilient RPC proxy |
| `cliffracer-faststream` | `FastStreamExtension` | FastStream broker hosting, resilient ACK/DLQ routing, and shutdown drain |
| `cliffracer-backdoor` | `BackdoorExtension` | the async debug backdoor |
| `cliffracer-cron` | *(none)* | `@cron` handlers. `CronTimer` subclasses core's `Timer`, so core's timer discovery starts them and there is nothing to declare |


Declaring an extension is what loads it, and that is the point of the split. It
is checked rather than assumed: with all ten extensions installed and all eight
of their third-party dependencies importable, `import cliffracer` leaves every
one of them absent from `sys.modules` — aioconsole, croniter, fastapi, jwt,
psutil, starlette, uvicorn, yaml, and the ten `cliffracer_*` packages
themselves. `tests/unit/test_core_imports_no_web_stack.py` contains the web stack
of that list to it on every run, in a fresh interpreter so the answer does not
depend on test order.

`psutil` and `aioconsole` are the two worth naming explicitly. They belong to
`cliffracer-backdoor`, the extension that evaluates arbitrary Python inside the
service process, so the debug console's dependencies arrive with that
distribution and stay with it.

Core binds two extensions of its own first, so they wrap every extension you
declare: `CorrelationExtension` sets the correlation id, and
`ValidationExtension` validates every RPC payload against its handler's
annotations and backs `@validated_listener`.

## The contract

Every hook is optional; the base class defines them all as no-ops.

| hook | when | notes |
|---|---|---|
| `setup(service)` | before the broker is connected | read config, **build per-instance state here** |
| `start()` | after the broker is connected and core subscriptions exist | |
| `stop()` | on shutdown, reverse declaration order, before the broker drains | |
| `worker_setup(ctx)` | before a handler runs | the only hook that can refuse |
| `worker_result(ctx, result, exc)` | after the handler, with what it returned or raised | |
| `worker_teardown(ctx)` | after `worker_result`, always | |
| `before_call(ctx)` | before this service **sends** a message | may add to `ctx.headers` |
| `after_call(ctx, result, exc)` | after the send, always | |
| `health_details()` | when `/health` is built | return `None` to contribute nothing |
| `info_details()` | when `/info` is built | |
| `entrypoint_kinds()` | at bind time, before `setup` | `kind -> binder`, for extensions that own a decorator |

`ctx` is a `WorkerContext`: `kind`, `subject`, `headers`, `correlation_id`,
`payload`, `raw`, and `data` — a per-dispatch dict for handing state from one
hook to the next.

### Two rules worth stating

**Build async resources and lifecycle state in `setup()`.** Extension class
attributes act as immutable factory specifications. `bind()` instantiates a
fresh runtime instance and deep-clones declaration arguments per service
instance, guaranteeing isolation. Async resources such as connections,
timers, or background workers belong in `setup()`. Both halves are pinned in
`tests/unit/test_extension_base.py`.

**Only `RejectMessage` can stop a handler, and only from `worker_setup`.**
Every other hook exception is logged under the extension's name and swallowed
— that is what stops a buggy metrics hook taking dispatch down, and it stays
true. The consequence to plan for is that **a bug in your hook fails silently**:
a `worker_teardown` that raises publishes nothing and returns no error to
anybody. Test the hooks against a real dispatch, not with a hand-built
`WorkerContext`.

Raised anywhere other than `worker_setup`, `RejectMessage` is swallowed like
anything else: by then the handler has run, and refusing afterwards is a lie.

### The send side

`worker_*` run around a message this service **consumes**. `before_call` and
`after_call` run around one it **sends**, with a `ctx.kind` per path:

| you call | `ctx.kind` | `result` in `after_call` |
|---|---|---|
| `call_rpc` | `call_rpc` | the reply's `result` |
| `call_async` | `call_async` | `None` |
| `call_rpc_no_wait` | `call_rpc_no_wait` | `None` |
| `publish_event` | `publish_event` | `None` |
| `broadcast_message` | `broadcast` | `None` |

`RpcProxy` goes through `call_rpc`, so it is covered by covering that.

Ordering is the same as the receive side: `before_call` in declaration order,
`after_call` in reverse, `after_call` always runs including when the send
raised, and a hook that raises is logged and changes nothing. There is **no
send-side `RejectMessage`** — no hook can cancel a send.

**`ctx.headers` is the one thing a hook may change**, and it is what these are
for: attach a token, add a trace id. The send path reads the headers back after
`before_call` and puts them on the wire.

```python
class AuthHeaderExtension(Extension):
    async def before_call(self, ctx):
        ctx.headers["authorization"] = f"bearer {self.token}"
```

`ctx.payload` and `ctx.subject` are read-only: the send paths pass a copy, so a
hook that writes to them changes nothing.

One message fires one pair. `broadcast_message` publishes internally, and it
runs a single `broadcast` chain rather than nesting a `publish_event` one
inside it — an outbound-latency hook counts a broadcast once.

These hooks see what the **service** sends. A handler that sends through some
other object — a connection it was handed, a client it constructed — is making
a call on that object, and no hook runs for it. There are two of those in the
tree: what a handler sends through `PoolExtension` is not seen by
`before_call` or `after_call`, and neither is a `ServiceClient` call, which
goes out on its own connection carrying the `headers=` it was constructed
with. An extension that attaches a token to outgoing messages does not attach
one to either.

## A worked example

This extension audits every dispatch and refuses unsigned messages. It is not
prose: it is quoted verbatim from
`tests/integration/test_extensions_guide.py`, which runs it against a real
broker, and `test_the_guide_quotes_this_file_verbatim` fails if this block and
that file drift apart.

```python
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext


class AuditExtension(Extension):
    """Publish one audit record per dispatch, and refuse unsigned messages.

    Declared on a service as a class attribute:

        class Orders(CliffracerService):
            audit = AuditExtension(require_signature=True)
    """

    def __init__(self, *, require_signature: bool = False) -> None:
        self.require_signature = require_signature
        # DECLARED here, CREATED in setup(). bind() is a shallow copy, so a
        # dict built in __init__ is the SAME object in every bound copy, and
        # two services would share each other's counts.
        self.counts: dict[str, int] | None = None

    async def setup(self, ctx) -> None:
        self.counts = {"ok": 0, "failed": 0, "refused": 0}

    async def worker_setup(self, ctx: WorkerContext) -> None:
        if self.require_signature and "x-signature" not in ctx.headers:
            # The ONE hook exception that stops the handler. Anything else
            # raised from any hook is logged under this extension's name and
            # swallowed, so a buggy hook cannot take dispatch down.
            self.counts["refused"] += 1
            raise RejectMessage("unsigned")
        # Hand state DOWN THE CHAIN, not onto self: ctx.data is per-dispatch,
        # and self is shared by every dispatch running concurrently.
        ctx.data["audit_started"] = True

    async def worker_result(self, ctx: WorkerContext, result, exc) -> None:
        if isinstance(exc, RejectMessage):
            return  # counted in worker_setup; a refusal is not a failure
        self.counts["failed" if exc is not None else "ok"] += 1

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        if not ctx.data.get("audit_started"):
            return  # refused before the handler ran: nothing happened to audit
        await self.service.publish_event(
            f"audit.{self.service.config.name}",
            kind=ctx.kind,
            # `on_subject`, not `subject`: publish_event takes the subject
            # positionally, so a `subject=` keyword collides with it and raises
            # -- and a hook exception is swallowed, so it fails invisibly.
            on_subject=ctx.subject,
        )

    def health_details(self) -> dict | None:
        # None before setup(): "not set up yet" is not a fault worth reporting
        # on /health, and the containers do not exist to report on.
        return None if self.counts is None else dict(self.counts)
```

Declared and used:

```python
from cliffracer import CliffracerService, rpc

class Orders(CliffracerService):
    audit = AuditExtension(require_signature=True)

    @rpc
    async def place(self, item: str) -> dict[str, str]:
        return {"placed": item}
```

What the tests pin, and why each one is there:

| test | pins |
|---|---|
| `test_a_signed_call_runs_and_is_audited` | the happy path, and that `worker_teardown` really publishes |
| `test_an_unsigned_call_is_refused_and_never_audited` | `RejectMessage` stops the handler **and** the refused message stays out of the audit trail |
| `test_a_handler_that_raises_is_counted_failed_not_ok` | a failure is counted as a failure and still audited: it reached the handler |
| `test_the_counts_reach_the_health_payload` | `health_details()` appears under the attribute name |

## Authentication, specifically

`AuthExtension` is worth its own note because declaring it changes the whole
service: the refusal is in `worker_setup`, before dispatch, so **every** handler
requires a valid token — decorated or not. `@requires_roles` narrows an
already-authenticated caller; it is not what turns authentication on.

```python
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer_auth import (
    AuthConfig,
    AuthExtension,
    SimpleAuthService,
    get_current_user,
    requires_roles,
)

auth_service = SimpleAuthService(AuthConfig(secret_key="a-secret-key-of-at-least-32-characters"))

class SecureService(CliffracerService):
    auth = AuthExtension(auth_service)

    @rpc
    async def whoami(self) -> dict[str, str]:
        return {"username": get_current_user().username}

    @rpc
    @requires_roles("admin")
    async def admin_only(self) -> dict[str, bool]:
        return {"ok": True}
```

`@rpc` outermost, `@requires_roles` beneath it: `@rpc` registers the subject,
so the guard has to sit between it and your function body.

What pins this, if you want to read the behaviour rather than trust it:
`packages/cliffracer-auth/tests/test_auth_context_reaches_the_handler.py`. Its
tests drive a real dispatch with a real token and no hand-set contextvar, which
is what makes them evidence about the extension.
`test_auth_decorators.py` sets the contextvar itself, so it says nothing about
whether anything else does.

**Writing your own issuer** is narrower than "return an `AuthContext` or
`None`": it must return a context with a user **and a future `expires_at`**.
`is_valid` is `False` when `expires_at` is `None`, so an issuer that omits it
refuses every caller and says nothing about why.

## OpenTelemetry Distributed Tracing (cliffracer-otel)

`OtelExtension` instruments incoming message handlers and outbound RPC / event calls
with OpenTelemetry spans and W3C traceparent propagation.

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer_otel import OtelExtension

class TracedService(CliffracerService):
    otel = OtelExtension()

    def __init__(self):
        super().__init__(ServiceConfig(name="traced_service"))
```

In `worker_setup`, `OtelExtension` extracts `traceparent` and `tracestate` headers from
incoming NATS messages, creates a `SERVER` span attached to the context, and tags it with
`cliffracer.subject`, `cliffracer.kind`, and `cliffracer.correlation_id`. In `worker_teardown`,
it ends the span and records any exceptions or message rejections.

For outbound calls (`call_rpc`, `call_async`, `publish_event`, `broadcast`), `before_call`
starts a `CLIENT` or `PRODUCER` span and injects the W3C `traceparent` header, which
`after_call` finishes when the round-trip completes.

Telemetry counters (`spans_total`, `errors_total`, `active_spans`) are exposed under `otel`
on `/health`.

## Key-Value and Object Store (cliffracer-kv)

`KvExtension` provides async access to NATS JetStream Key-Value buckets and Object Stores.

```python
from pydantic import BaseModel
from cliffracer import CliffracerService, ServiceConfig
from cliffracer_kv import BucketConfig, KvExtension

class UserRecord(BaseModel):
    user_id: str
    active: bool

class StorageService(CliffracerService):
    kv = KvExtension(
        buckets=[
            BucketConfig(name="users", ttl=7200),
            "cache",
        ],
        bucket_ttls={"cache": 300},
        object_stores=["documents"],
    )

    def __init__(self):
        super().__init__(ServiceConfig(name="storage_service", jetstream_enabled=True))

    async def store_user(self, record: UserRecord) -> int:
        return await self.kv.put("users", record.user_id, record)

    async def fetch_user(self, user_id: str) -> UserRecord | None:
        return await self.kv.get("users", user_id, as_type=UserRecord)
```

Buckets and object stores are provisioned at service startup. The extension supports
bucket-level TTL configuration directly through `BucketConfig(name=..., ttl=...)` or
the `bucket_ttls` mapping, passing the TTL parameter to JetStream bucket creation.
Serialization handles Pydantic models, dictionaries, strings, and raw bytes, with
optimistic concurrency control via revision numbers.

## Resilience: Circuit Breaking and Rate Limiting (cliffracer-resilience)

`cliffracer-resilience` provides circuit breaking and rate limiting for protecting
microservices against cascading failures and traffic spikes.

```python
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer_resilience import (
    CircuitBreakerConfig,
    ResilienceExtension,
    ResilientRpcProxy,
    rate_limit,
)

class OrderService(CliffracerService):
    resilience = ResilienceExtension()
    payments = ResilientRpcProxy(
        "payment_service",
        config=CircuitBreakerConfig(
            failure_threshold=5,
            recovery_timeout=30.0,
            half_open_max_calls=1,
        ),
    )

    def __init__(self):
        super().__init__(ServiceConfig(name="order_service"))

    @rpc
    @rate_limit(calls=50, window=60)
    async def create_order(self, order_id: str, amount: float) -> dict[str, str]:
        charge = await self.payments.charge(order_id=order_id, amount=amount)
        return {"order_id": order_id, "status": "confirmed", "charge": str(charge)}
```

`ResilientRpcProxy` implements a three-state machine (`CLOSED`, `OPEN`, `HALF_OPEN`). When
downstream failures exceed `failure_threshold`, it trips to `OPEN` and fast-fails outbound
calls locally without sending wire traffic. After `recovery_timeout`, it transitions
to `HALF_OPEN` to permit a probe request before restoring normal service.

`@rate_limit` decorates RPC and listener handlers with call count thresholds per time window.
`ResilienceExtension` enforces these limits in `worker_setup` using either in-memory sliding
windows (`InMemoryRateLimiter`) or distributed JetStream KV stores (`KvRateLimiter`). Calls
exceeding the threshold raise `RejectMessage`, which the container translates to a wire
refusal response without running the handler.

## Auto-Gateway for Dynamic RPC Ingress (cliffracer-http)

`AutoGatewayExtension` dynamically mounts FastAPI HTTP endpoints backed by Cliffracer
RPC services, handling verb inference, parameter binding, NATS RPC dispatch, error
translation, and interactive OpenAPI documentation.

### Service Definition with RPC Handlers

Define RPC services with typed request and response models:

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
        """Fetch a user by id."""
        return UserModel(id=user_id, name="Alice", email="alice@example.com")

    @rpc
    async def create_user(self, payload: CreateUserPayload) -> UserModel:
        """Create a new user."""
        return UserModel(id="usr_123", name=payload.name, email=payload.email)

    @rpc
    async def delete_user(self, user_id: str) -> bool:
        """Delete a user by id."""
        return True
```

### Automatic Verb and Endpoint Mapping

`AutoGatewayExtension` inspects method names and parameters, automatically mapping them to HTTP routes:

- **`get_user`** -> `GET /api/v1/users/get_user?user_id=...`
  Methods prefixed with `get_`, `list_`, `fetch_`, `find_`, `read_`, `search_`, or `query_` infer the `GET` HTTP verb. Scalar parameters are extracted as HTTP query parameters.
- **`create_user`** -> `POST /api/v1/users/create_user`
  Methods prefixed with `create_`, `add_`, `post_`, `insert_`, `register_`, or `new_` infer the `POST` HTTP verb. Pydantic request models (`CreateUserPayload`) are validated and parsed from the incoming JSON request body: `{"name": "...", "email": "..."}`.
- **`delete_user`** -> `DELETE /api/v1/users/delete_user?user_id=...`
  Methods prefixed with `delete_`, `remove_`, `drop_`, `clear_`, or `cancel_` infer the `DELETE` HTTP verb.
- Methods prefixed with `update_`, `set_`, `put_`, `modify_`, or `replace_` infer `PUT`, and `patch_` infers `PATCH`.
- Any unrecognized method prefix defaults to `POST`.
- Custom mappings can be configured via `verb_overrides={"method": "VERB"}` and `path_overrides={"method": "/custom/path"}`.

### Dedicated Gateway for Downstream Services

To front multiple downstream services with a dedicated gateway service, pass downstream service classes in `targets`:

```python
from cliffracer import CliffracerService
from cliffracer_http import AutoGatewayExtension, HttpExtension


class ApiGateway(CliffracerService):
    name = "api_gateway"
    http = HttpExtension(port=8080)
    gateway = AutoGatewayExtension(
        targets=[UserService],
        prefix="/api/v1",
    )
```

### Swagger and OpenAPI Documentation

FastAPI generates interactive Swagger documentation and OpenAPI schemas automatically:

- Interactive Swagger UI: `http://localhost:8080/docs`
- ReDoc documentation: `http://localhost:8080/redoc`
- OpenAPI JSON schema: `http://localhost:8080/openapi.json`

Every mounted RPC route includes Pydantic input and output schemas, parameter descriptions, method docstrings, and tags grouped by service name.

### Error Translation

HTTP calls are dispatched over NATS RPC to downstream services and translated to standard HTTP response codes:

| Condition | Status Code | Response Body |
|---|---|---|
| RPC succeeds | `200 OK` | Serialized return model (e.g. `UserModel`) |
| Validation error / `RPCError` with `details` | `422 Unprocessable Entity` | `{"detail": {"error": "...", "details": [...]}}` |
| RPC execution failure / unhandled `RPCError` | `502 Bad Gateway` | `{"detail": "RPC error calling users.get_user: ..."}` |
| Downstream timeout (`RPCTimeoutError`) | `504 Gateway Timeout` | `{"detail": "Gateway timeout calling users.get_user: ..."}` |
| Internal gateway failure | `500 Internal Server Error` | `{"detail": "Internal gateway error calling users.get_user: ..."}` |

## FastStream Broker Hosting (cliffracer-faststream)

`FastStreamExtension` allows Cliffracer to act as an operational hypervisor for legacy FastStream routers. It surfaces Cliffracer's powerful NATS-native features—like resilient message acknowledgment, JetStream Key-Value stores, circuit breakers, and bounded shutdown drains—directly into FastStream's `ContextRepo`.

### Usage

Mount an existing FastStream router using the extension:

```python
from faststream.nats import NatsRouter
from cliffracer import CliffracerService
from cliffracer.core.extension import SharedDependency
from cliffracer_faststream import FastStreamExtension

router = NatsRouter()

@router.subscriber("legacy.events")
async def handle_event(msg: dict):
    # This FastStream handler now benefits from Cliffracer's resilient ACK middleware
    print(f"Processed: {msg}")

class HybridService(CliffracerService):
    name = "hybrid_service"
    faststream = FastStreamExtension(router=SharedDependency(router))
```

The extension automatically shares the underlying NATS connection, coordinates the FastStream shutdown lifecycle with Cliffracer's core drain loop, and replaces FastStream's destructive `REJECT_ON_ERROR` behavior with resilient JetStream NAK/TERM routing.

## Binary Serialization (MsgPack) Support

Cliffracer core supports binary MessagePack serialization as an alternative to JSON for
lower latency and reduced payload size.

Enable it by setting `serialization_format="msgpack"` in `ServiceConfig`:

```python
from cliffracer import CliffracerService, ServiceConfig, rpc

class DataService(CliffracerService):
    def __init__(self):
        super().__init__(
            ServiceConfig(
                name="data_service",
                serialization_format="msgpack",
            )
        )

    @rpc
    async def process_batch(self, items: list[str]) -> dict[str, int]:
        return {"processed": len(items)}
```

Payloads are packed with `pack_msgpack` and decoded with `unpack_msgpack`. Services
inspect the `content-type` header (`application/msgpack` vs `application/json`), automatically
negotiating formats across RPC proxies and clients.

## Packaging an extension

Each extension is its own distribution under `packages/`, a workspace member,
versioned in lockstep with `cliffracer`.

Two lines in a member's `pyproject.toml` are strictly required and only one of them
looks it:

- **`readme = "README.md"` must be declared.** Having the file is not enough:
  hatchling sweeps it into the sdist by default, so it looks present while
  being the long description of nothing — absent from the wheel's `METADATA`,
  blank on the registry page and in `pip show`.
- **`LICENSE` needs no declaration, but must be a real copy.** Hatchling
  auto-detects a `LICENSE` beside the pyproject, so a `license-files` entry is
  inert. It must be a copy and not a symlink: a symlink to `../../LICENSE`
  builds a wheel fine and then fails unpacking the sdist with
  `symlink destination for ../../LICENSE is outside of the target directory`.

In one sentence: *a member needs `readme = "README.md"` in its pyproject and a
copy of `LICENSE` beside it; the licence needs no declaration and the readme is
useless without one.*

Guarded by `tests/unit/test_member_wheels_carry_metadata.py`, which reads the
built artefacts rather than the source tree — the wheel's `METADATA` for a
non-empty description and `dist-info/licenses/` for the licence, and the sdist
separately, because the symlink defect appears only there.

## Installing

```bash
pip install cliffracer cliffracer-http cliffracer-metrics
```

`cliffracer run --config <file>` needs PyYAML, which core does not depend on —
install `cliffracer[cli]` if you are installing core **alone**. In a full
install it arrives transitively through `cliffracer-http`'s
`uvicorn[standard]`, so a consumer will not discover the extra until they
install core by itself.

## See also

- [api-reference.md](api-reference.md) — the `ServiceConfig` field table
- [ARCHITECTURE.md](ARCHITECTURE.md) — why extensions rather than mixins
- `packages/*/README.md` — one per distribution
