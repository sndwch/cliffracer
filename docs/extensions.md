# Extensions

An extension is an object declared as a class attribute on a service, bound
per service instance, and run by the container. **`setup`, `start` and
`worker_setup` run in declaration order; `worker_result`, `worker_teardown` and
`stop` run in reverse**, so an extension is torn down inside the ones declared
before it. Every hook is optional.

```python
from cliffracer import CliffracerService
from cliffracer_logging import LoggingExtension
from cliffracer_metrics import MetricsExtension

class Orders(CliffracerService):
    logging = LoggingExtension()
    metrics = MetricsExtension()
```

The attribute name does two jobs. It is how you reach the extension
(`self.metrics`), and the key its contributions appear under in `/health` and
`/info`.

## The distributions

| distribution | attribute type | provides |
|---|---|---|
| `cliffracer-auth` | `AuthExtension` | JWT auth, `@requires_auth` / `@requires_roles` / `@requires_permissions` |
| `cliffracer-logging` | `LoggingExtension` | structured logging, correlation logging, log-to-NATS |
| `cliffracer-metrics` | `MetricsExtension` | in-process counters over the dispatch hooks |
| `cliffracer-otel` | `OtelExtension` | OpenTelemetry distributed tracing and W3C context propagation |
| `cliffracer-kv` | `KvExtension` | NATS JetStream Key-Value and Object Store with bucket TTL |
| `cliffracer-resilience` | `ResilienceExtension` | circuit breaking, sliding-window rate limiting, resilient RPC proxy |
| `cliffracer-cron` | *(none)* | `@cron` handlers. `CronTimer` subclasses core's `Timer`, so core's timer discovery starts them and there is nothing to declare |
| `cliffracer-dlq` | *(none)* | `cliffracer-dlq`, a read-only command for the dead letters in their stream. It is a tool for operators and declares nothing on a service |
| `cliffracer-cyanide` | `CyanideExtension` | fault injection for testing: delay, raised faults, simulated timeouts and dropped replies |


Declaring an extension is what loads it, and that is the point of the split. It
is checked rather than assumed: `import cliffracer` leaves every `cliffracer_*`
package absent from `sys.modules`, and none of the third-party modules the
extensions need, such as croniter, jwt and opentelemetry, is loaded either.
`tests/repo/test_core_imports_no_web_stack.py` holds the web-stack part of that
to it on every run, in a fresh interpreter so the answer does not depend on
test order.

Core binds two extensions of its own around the ones you declare.
`CorrelationExtension` is first and sets the correlation id for every extension
you declare. `ValidationExtension` is last and validates every RPC and async RPC
payload against its handler's annotations, so a gate you declare (authentication,
a rate limit) refuses a message whose payload decodes before any schema diagnostic
is produced for it, and your validators never see input a gate turned away. Decoding
comes first and is the dispatcher's own: a payload that cannot be decoded (a body that
is not JSON or msgpack) reaches no declared extension. An RPC with one is answered
`validation_failed` with the decoder's text under the default `rpc_validation_errors`
policy, a fire-and-forget request is logged and dropped, and an event is dead-lettered;
a gate is not consulted for it and a limit spends no permit on it. Hold that in mind
when a gate must see every message: it sees every message that decodes. It does nothing for events: a `@validated_listener` or
typed `@listener` payload is validated by the event dispatcher, and a message
that fails is dead-lettered or dropped according to the listener's `on_invalid`,
else the service's `default_on_invalid` (see
[Message Validation](api-reference.md#message-validation)).

## The contract

Every hook is optional; the base class defines them all as no-ops.

| hook | when | notes |
|---|---|---|
| `setup(service)` | before the broker is connected | read config, **build per-instance state here** |
| `start()` | after the broker is connected and `on_startup` has returned, before the service subscribes to its handlers | |
| `stop()` | on shutdown, reverse declaration order, before the broker drains | pairs with `setup()`, not `start()`: it also runs when startup stopped before `start()` |
| `worker_setup(ctx)` | before a handler runs | the only hook that can refuse |
| `worker_result(ctx, result, exc)` | after the handler, with what it returned or raised, or after a refusal | |
| `worker_teardown(ctx)` | after `worker_result`, always | also runs for an extension whose `worker_setup` never ran because an earlier one refused |
| `before_call(ctx)` | before this service **sends** a message | may add to `ctx.headers` |
| `after_call(ctx, result, exc)` | after the send, always | |
| `health_details()` | when `/health` is built | return `None` to contribute nothing |
| `info_details()` | when `/info` is built | |
| `on_disconnect()` | the broker connection was lost, before the client tries to reconnect | **quick, or hand slow work to a task**: the client waits for it before it reconnects |
| `on_reconnect()` | the connection was regained, subscriptions replayed | traffic already flows while it runs; hand long work to a task |
| `on_listener_paused(subject, dependencies)` | the service stopped consuming a `pause_when_down` listener because `dependencies` are down | |
| `on_listener_resumed(subject, dependencies)` | it consumes that listener again: `dependencies` are all up | |

`health_details()` is informational. What it returns is added to the `/health` body under the
extension's name, and a `health_details()` that raises is reported there as an `error` entry; neither
changes the service's status or its HTTP status code, so an extension that reports itself stopped
still leaves the service `healthy`. An extension that must take the service out of rotation
declares a dependency probe (`service.add_dependency(...)`), which is what decides readiness.

`ctx` is a `WorkerContext`: `kind`, `subject`, `headers`, `correlation_id`,
`payload`, `raw`, and `data` — a per-dispatch dict for handing state from one
hook to the next.

`ctx.payload` is the decoded wire payload as it arrived: not coerced to the
handler's parameter types, and with `correlation_id` still in it. Every hook sees
that form, because validation does not change it. The arguments an RPC handler
receives, validated and coerced, are in `ctx.data["validated_kwargs"]` once
`ValidationExtension`'s `worker_setup` has run. That is after every extension you
declare has run its own `worker_setup`, so a declared extension reads them in
`worker_result`, not in `worker_setup`.

An event that fails its schema is dead-lettered or dropped and raises nothing, so `worker_result`
receives `exc=None` for it. The dispatch is marked `ctx.data["outcome"] = "invalid"`, which is how
`MetricsExtension` counts it as `rejected` and `OtelExtension` ends its span in error, as the
refusal an invalid RPC raises does.

### Hearing the connection go and come back

`on_disconnect()` and `on_reconnect()` run when the client reports the connection lost and regained,
in declaration order, each before the matching `ServiceConfig.on_disconnect` / `on_connect` slot. A
hook that raises is logged and the rest still run.

The client awaits `on_disconnect()` before it reconnects, so a hook that waits holds the reconnect up
by as long as it waits: keep it quick, or start a task and return. `on_reconnect()` runs once the
connection is back and the subscriptions are replayed, so a slow one does not stall traffic, but hand
its long work to a task as well. On reconnect the client replays every
subscription it holds; a subscription unsubscribed inside `on_disconnect()` is not replayed, which is
the place to drop a subject that must not come back on its own. A lost connection is noticed when the
socket breaks or the client's ping timeout expires, so `on_disconnect()` can run well after the
connection stopped working: use it to react, not as a fence.

### What runs when startup does not finish

An extension's `stop()` pairs with its `setup()`. `setup()` is where its resources are
built, so a startup that fails or is stopped after `setup()` and before `start()` still calls
`stop()`; `stop()` must tolerate an extension whose `start()` never ran. An extension whose `setup()` raised is stopped too, because it may hold part of what it was building. One declared after it, whose `setup()` was never begun, is not stopped, and neither is any extension of a service that never began its setup. The service's own
`on_shutdown` pairs with `on_startup` the other way round: it runs only when `on_startup`
returned, so an `on_startup` that raises or is cancelled has to release what it built
itself. It also runs for a stop that was cancelled before it got there, for example by the
close of the broker connection, shielded and bounded by `shutdown_timeout`, or by a fixed 30 seconds when that is `None`: past
that bound a hung `on_shutdown` is abandoned and logged, so a stop can always be ended. Both pairings are pinned in `tests/unit/test_the_startup_and_shutdown_hook_contract.py`.

### Two rules worth stating

**Build async resources and lifecycle state in `setup()`.** Extension class
attributes act as immutable factory specifications. `bind()` instantiates a
fresh runtime instance and deep-clones declaration arguments per service
instance, guaranteeing isolation. Async resources such as connections,
timers, or background workers belong in `setup()`. Both halves are pinned in
`tests/unit/test_extension_base.py`.

**A zero-argument callable passed as an extension argument is CALLED once per
bound instance, and the extension receives its result.** That is what makes a
factory work — `Extension(client=make_client)` gives each service its own
client — and it applies to anything callable with no arguments, including a
callable OBJECT, which is invoked through `__call__`. If you meant to pass the
callable itself, wrap it: `SharedDependency(my_callback)`.

A callable that needs arguments is copied rather than called, and one whose
factory raises fails the bind with `ExtensionIsolationError` naming it. So is a C
callable whose signature cannot be read, or reads only as `(*args, **kwargs)` with
no Python code behind it (`operator.itemgetter`, `attrgetter`, `methodcaller` and
`sqlite3.Connection` read that way on Python 3.12): it is copied, not called.

Lists, dicts and tuples are copied item by item, at any depth, so these rules hold for
what is inside them: `routers=[SharedDependency(router), make_client]` shares `router` and
calls `make_client`. (A tuple subclass such as a `NamedTuple` is copied the same way and keeps its type; one whose
constructor cannot be built from its items is copied whole, with a `RuntimeWarning` when a `SharedDependency`, a
factory or an extension is inside it. A subclass of `list` or `dict` is copied whole, as any other object.)
Every other argument is deep-copied for each bound instance, and a nested
extension is built fresh. An argument that cannot be copied (a lock, a client
holding sockets) fails the bind with `ExtensionIsolationError` rather than being
shared by reference; wrap it in `SharedDependency(obj)` when sharing one object
across instances is what you mean.

**Plain data is the only thing copied without a word.** Plain data is a list, dict, set,
frozenset or tuple, a pydantic model or dataclass instance, and the value types (strings,
bytes, numbers, `None`, dates and times, `Decimal`, `UUID`, paths, ranges). Any other object
that is copied, such as an unconnected `nats.NATS()` client, a store or a limiter, is copied
with a `FutureWarning` that names the extension and the argument, and a future release will
refuse it at bind unless it is wrapped in `SharedDependency(obj)` (one object for every
service) or given as a zero-argument callable (one built for each). An object that must be
one per process should be written that way now.

**A declaration is frozen.** The extension a class body declares is a
specification, and it is frozen when the class body declares it: `freeze()` copies its
arguments, so changing a declared list or dict in place afterwards cannot reach
an instance built later, and assigning or deleting an attribute on the
declaration raises `AttributeError`. One passed to `add_extension` is frozen
when it is bound. State that belongs to a service lives on the bound instance,
which is not frozen: build it in `setup()`. The isolation guarantee covers a specification's public attributes and
its arguments; its underscore-prefixed attributes, `_spec_args` and `_spec_kwargs` among them, are the machinery
that does the freezing and are not part of it, so a write made through one is not guarded.

**Only `RejectMessage` can stop a handler from an extension, and only from `worker_setup`.**
Every other hook exception is logged under the extension's name and swallowed
— that is what stops a buggy metrics hook taking dispatch down, and it stays
true. The consequence to plan for is that **a bug in your hook fails silently**:
a `worker_teardown` that raises publishes nothing and returns no error to
anybody. Test the hooks against a real dispatch, not with a hand-built
`WorkerContext`.

Hooks are shown here as `async def` and that is the shape to write, but a plain
`def` override is accepted and isolated the same way: it is called inside the
guard and its return awaited only if it is awaitable.

Raised by a hook other than `worker_setup`, `RejectMessage` is swallowed like
anything else: by then the handler has run, and refusing afterwards is a lie.

Raised by the handler body itself, it is honoured as a refusal: an RPC caller
gets the refusal reply, and an event is neither retried nor dead-lettered (a
core event ends `OK`, a JetStream message is acknowledged), so a handler that
has decided a message must never be redelivered says so by raising it.
`worker_result` receives it as `exc`.

A timer or cron firing has no caller to answer, so a refusal from `worker_setup` skips the method and
is reported at WARNING without a traceback. The timer counts it in `refusal_count` (with the reason in
`last_refusal`), and not in `error_count`, the error rate or its executions; a distributed cron
firing is recorded in its interval record with status `refused` and the reason. A gate that
crashes is not a refusal: the firing counts in `error_count` with the crash in `last_error`, is
logged at ERROR with its traceback, and a distributed cron firing is recorded with status `failed`.
A fire-and-forget (`@async_rpc`) request is treated the same way: a refusal is logged at WARNING and
a crashed gate at ERROR.

**Teardown is not paired with setup on a refusal.** When an extension refuses in
`worker_setup`, the extensions declared after it never had their `worker_setup`
called, but they still receive `worker_result` (with the refusal as `exc`) and
`worker_teardown`. An extension that releases in `worker_teardown` what it
acquired in `worker_setup` has to cope with finding nothing to release; reading
per-dispatch state with `ctx.data.pop(key, None)` is the pattern.

`RetryMessage` is the transient form of `RejectMessage`. RPC callers receive a
normal refusal, with its `retry_after` when that is a finite number of zero or more, while durable event consumers NAK the delivery after its
`retry_after` delay, which is a finite number of seconds above zero: `None`, zero, a negative
number, `nan` and `inf` are no hint, and the NAK uses the exponential backoff the consumer is
configured with. A delivery that has reached the consumer's limit is
dead-lettered and terminated. Use it for capacity decisions that can change
without changing the message; authentication and validation remain ordinary,
terminal `RejectMessage` decisions.

**The `reason` string reaches the caller verbatim**, and deliberately outside
`expose_internal_errors` — a refusal is an answer to whoever sent the message,
not an internal error. So an extension that writes
`raise RejectMessage(f"auth failed: {exc}")` is publishing `exc` to anyone who
can reach the subject. Write reasons for the caller.

**Fail closed when your hook is a gate.** Set `fails_closed = True` on the
extension class (`AuthExtension`, `ValidationExtension` and the resilience and
cyanide extensions do). An exception that escapes its `worker_setup` then
refuses the message instead of being swallowed, so the handler cannot run
unchecked. A hook that is not a gate leaves it `False`, and its failure is
logged and the message proceeds.

That gate does still apply to the refusal the framework SYNTHESISES when a
`fails_closed` hook crashes: that one is the service being broken rather than
the caller being turned away, and its text goes through the same gate a handler
exception does. It is reported as `internal`, not `refused`, and a durable event
it refuses takes the handler-failure path: NAK, and the dead-letter queue once
the delivery limit is spent, where a refusal an extension authored is
acknowledged.

### The send side

`worker_*` run around a message this service **consumes**. `before_call` and
`after_call` run around one it **sends**, with a `ctx.kind` per path:

| you call | `ctx.kind` | `result` in `after_call` |
|---|---|---|
| `call_rpc` | `call_rpc` | the reply's `result` |
| `call_async` | `call_async` | `None` |
| `call_rpc_no_wait` | `call_rpc_no_wait` | `None` |
| `stream_rpc` | `stream_rpc` | `None`; the hooks run once, around opening the stream |
| `publish_event` | `publish_event` | `None` |
| `broadcast_message` | `broadcast` | `None` |

`RpcProxy` goes through `call_rpc`, and its `.stream(...)` through `stream_rpc`, so it is covered by covering those.

Ordering is the same as the receive side: `before_call` in declaration order,
`after_call` in reverse, `after_call` always runs including when the send
raised, and a hook that raises is logged and changes nothing. There is **no
send-side `RejectMessage`** — no hook can cancel a send.

**`ctx.headers` is the one thing a hook may change**, and it is what these are
for: attach a token, add a trace id. The send path reads the headers back after
`before_call` and puts them on the wire.

```python
from cliffracer import Extension


class AuthHeaderExtension(Extension):
    async def before_call(self, ctx):
        ctx.headers["authorization"] = f"bearer {self.token}"
```

For a service-identity bearer token, `AuthExtension` does this itself: give it
`outbound_token_factory`, a callable returning a token (or an awaitable of one),
and every call, async call and published event carries
`authorization: Bearer <token>`, the factory being called once per message sent.
A header the message already carries is left alone, a factory that returns
nothing sends the message without one, and one that raises is logged and the
message goes without one, since no hook can cancel a send. The caller's own token
is never forwarded. See the `cliffracer-auth` README.

`ctx.payload` and `ctx.subject` are read-only. Reassigning `ctx.subject` changes
nothing, and the payload's dicts, lists, tuples and sets are copies, so writing
to them changes nothing either. A custom object inside the payload, such as a
pydantic model, is not copied: setting one of its attributes changes the
caller's object. It does not change the message on any path: every send path
serialises the payload before `before_call` runs. A payload the
serialiser refuses raises before any hook runs. Serialising reads a one-shot
iterable in the payload, such as a generator, so a hook sees it already
consumed.

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
broker, and `tests/repo/test_the_extensions_guide_quotes_the_example.py` fails
if this block and that file drift apart.

```python
from cliffracer import Extension, RejectMessage, WorkerContext


class AuditExtension(Extension):
    """Publish one audit record per dispatch, and refuse unsigned messages.

    Declared on a service as a class attribute:

        class Orders(CliffracerService):
            audit = AuditExtension(require_signature=True)
    """

    def __init__(self, *, require_signature: bool = False) -> None:
        self.require_signature = require_signature
        # DECLARED here, CREATED in setup(). bind() runs __init__ again for
        # each service, so a dict built here would not be shared either; the
        # counts start in setup() because that is where per-service state
        # begins, and where a connection, timer or worker would have to go.
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
requires a valid token — decorated or not — except a `@timer` firing that carries
no token while `allow_timers` is True (the default); see the auth README for the
three ways to give a timer an identity. `@requires_roles` narrows an
already-authenticated caller; it is not what turns authentication on. Given
several roles it admits a caller who holds any one of them; stack the decorators
to require all. A caller it turns away is refused (`refused: forbidden`), not told
the service failed, and the roles it required are in the log, not the reply.

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

The extension keeps `auth_service` by identity because it owns live security
state. `auth_service.revoke_token(...)` affects the next validation on every
already-created service. Calls to `create_user(...)`, `add_role(...)`, and
`add_permission(...)` remain available to subsequent authentication and refresh
operations. Several services may intentionally share one issuer.

`@rpc` outermost, `@requires_roles` beneath it: `@rpc` registers the subject,
so the guard has to sit between it and your function body.

What pins this, if you want to read the behaviour rather than trust it:
`packages/cliffracer-auth/tests/test_auth_context_propagation.py`. Its
tests drive a real dispatch with a real token and no hand-set contextvar, which
is what makes them evidence about the extension.
`test_auth_decorators.py` sets the contextvar itself, so it says nothing about
whether anything else does.

**Writing your own issuer** is narrower than "return an `AuthContext` or
`None`": it must return a context with a user **and a future `expires_at`**.
`is_valid` is `False` when `expires_at` is `None`, so an issuer that omits it
refuses every caller and says nothing about why. `validate_token` may be
`async def`; the extension awaits it.

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
incoming NATS messages and starts a span attached to the context, named `{kind} {handler}` (`rpc get_order`,
`event on_order`) so that every subject reaching one handler is one group in a tracing backend. An `event`
delivery is a `CONSUMER` span, a `timer` span is `INTERNAL`, and `rpc` and `async_rpc` spans are `SERVER`. A `describe` request starts no
span and is counted in neither `spans_total` nor `errors_total`. The span is tagged with
`cliffracer.subject`, `cliffracer.kind`, and `cliffracer.correlation_id`, plus `messaging.system`
(`nats`), `messaging.destination.name` (the subject) and `messaging.operation.type` (`process`)
when the broker delivered the message. In `worker_result`,
it records any exception or message rejection on the span and sets its status; in
`worker_teardown` it ends the span and detaches the context.

For outbound calls (`call_rpc`, `call_async`, `publish_event`, `broadcast`), `before_call`
starts a `CLIENT` or `PRODUCER` span carrying the same messaging attributes (operation `send`) and injects the W3C `traceparent` header, which
`after_call` finishes when the round-trip completes. For `stream_rpc` that is when the stream
is opened, so the span covers the request and not the items that follow.

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
exceeding the threshold raise `RateLimitExceeded`. The container returns a wire
refusal for RPC, while durable listeners NAK the event until the sliding window
has capacity, without running the handler.
A limit counts delivery attempts: a JetStream redelivery spends another permit, and a permit spent
on a message that a later extension refuses is not returned. A limit is checked before the payload
is validated, so a payload that decodes and fails validation spends a permit of it, and is
answered `validation failed` while the limit has one and `rate limit exceeded` once it has none;
a payload that cannot be decoded is refused before any limit and spends none. A `@rate_limit` function that is called directly, not dispatched, counts against its
own in-memory limiter unless its decorator is given `limiter=`; the extension's limiter does not
reach it.

A `RateLimiter` passed as `ResilienceExtension(limiter=...)` is the one exception to the rule that an
argument is copied for each bound instance: it copies itself as itself, so every service built from
the declaration shares it without `SharedDependency`. Without `limiter`, each service gets its own
`InMemoryRateLimiter`.

String partition keys read headers by default. Declare
`key_source="payload"` to use a caller-controlled payload field explicitly;
payload values cannot shadow authenticated headers. KV key names and logs use
only a SHA-256 fingerprint of the resolved value. `KvRateLimiter` fails closed
when its shared state is unavailable or invalid. Its optional
`in_memory_fallback=True` mode is visible as degraded state and a fallback count
under the extension's health details because it relaxes the cluster-wide bound.
Handler-specific limiter health appears under `handler_rate_limiters` by handler
name.

`/health` also reports, under the extension's name, `rate_limits` (per handler, the dispatches a
limit let through and the ones it refused, and the totals; a payload refused by validation counts in
`permitted`, because the limit let it through first), `tracked_keys` beside an in-memory limiter, and `circuits` (the destination,
state, failure count and seconds in state of each `ResilientRpcProxy` the service declares).
`/info` lists each handler's `calls`, `window` and key source, never a key value, the limiter
class and the default limit.

## Connection Pool (cliffracer-metrics)

`PoolExtension` keeps a pool of NATS connections beside the service's own. It lives in the
`cliffracer-metrics` distribution, with `MetricsExtension` and `BatchProcessor`, and a service that
wants a pool installs that distribution.

Each pooled connection follows the service's configuration. `ServiceConfig.nats_connect_kwargs()`
gives the credentials and the `nats_inbox_prefix`, and the service's own connection takes them from
the same method, so a pooled `request` is answered on the inbox prefix the service's broker user is
allowed to subscribe to. The pool also takes the service's `connect_timeout`, which bounds each
connection, and its `max_reconnect_attempts`, `reconnect_time_wait`, `ping_interval` and `max_outstanding_pings`
(unset on the service, nats-py's defaults) unless the extension is given its own. Each connection is named `<service>-pool-<n>` on the broker. `PoolExtension.request` and `publish` send the correlation id of the request being handled in `X-Correlation-ID` and `correlation_id` (a new one when there is none, and one the caller passes in `headers=` wins), so the service that answers is a hop of the same trace; the send hooks do not run for pool traffic. A connection that nats-py
closes for good, because its reconnect attempts ran out, is logged at WARNING and counted
(`closed_connections` in the pool's `get_stats()`) and is skipped when the next connection is chosen; the errors nats-py
reports for a pooled connection are logged at ERROR and counted (`connection_errors`). The
disconnects and reconnects before a permanent close are not logged; the service's own connection
logs them.

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

The two formats carry the same values, so choosing one changes the encoding and never what a
handler receives. A payload is normalised as for JSON before it is packed: integer map keys
become strings, `bytes` become `str`, and bytes that are not valid UTF-8 are refused when the
message is sent, under either format.

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

Guarded by `tests/repo/test_member_wheels_carry_metadata.py`, which reads the
built artefacts rather than the source tree — the wheel's `METADATA` for a
non-empty description and `dist-info/licenses/` for the licence, and the sdist
separately, because the symlink defect appears only there.

## Installing

```bash
pip install cliffracer cliffracer-auth cliffracer-metrics
```

`cliffracer run --config <file>` needs PyYAML, which core does not depend on and
no extension brings in — install `cliffracer[cli]`.

## See also

- [api-reference.md](api-reference.md) — the `ServiceConfig` field table
- [ARCHITECTURE.md](ARCHITECTURE.md) — why extensions rather than mixins
- `packages/*/README.md` — one per distribution
