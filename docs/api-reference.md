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
- `@idempotent(key="...")` - Deduplicated event publishing via JetStream

**From `cliffracer_cron` (the `cliffracer-cron` distribution; `cliffracer` does not export it):**
- `@cron("expr", tz="UTC")` - Run a method on a cron schedule

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

`@cron(expression, tz="UTC", eager=False, headers=None, token_factory=None, *, distributed=False, bucket="cron_locks", lease_ttl=300.0, no_overlap=True)`:
- `expression` — any cron expression (`"min hour dom month dow"`) or named schedule
  (`@hourly`, `@daily`, `@weekly`, ...). Validated when the decorator is applied — a bad
  expression raises `ValueError` immediately, not silently at runtime. That includes one with
  valid syntax that names no date, such as `0 0 30 2 *` (30 February) or `0 0 31 4 *`.
- `tz` — IANA timezone the expression is evaluated in (default `"UTC"`). Invalid names raise `ValueError`.
- `eager` — if `True`, also run once on service start. With `distributed=True` it runs once per cluster for as long as the bucket keeps the `.eager` key, not on every start. That is the bucket's TTL, `max(lease_ttl, 300)` seconds for a bucket the timer creates and whatever an existing bucket has: a restart, or a rolling deploy, inside that window does not run it again, on this replica or any other, and on a bucket with no TTL it never runs again. Work that must run after every start, a cache warm-up or a reconciliation, belongs in the service's `on_startup`.
- `headers`, `token_factory` — as for `@timer`: fixed headers, or a callable returning a bearer token, that each firing carries.
- `distributed` — if `True`, replicas coordinate through `cliffracer-kv` so one runs each firing; the service must declare a `KvExtension`, and one that does not is refused when it starts. `bucket` (default `"cron_locks"`) names the Key-Value bucket for the distributed locks and execution records, `lease_ttl` (default `300.0` seconds) is how long a running lease is honoured before another run starts over it (a job with `no_overlap` whose `lease_ttl` is longer than the TTL of its bucket is refused when it starts), and the lease and the interval records are keys in the bucket and live its TTL, `max(lease_ttl, 300)` seconds for a bucket the timer creates and whatever an existing bucket has. A `lease_ttl` that is not a finite number of seconds above zero, a `no_overlap` that is not a bool and a `bucket` the Key-Value layer would refuse raise `ConfigurationError` when the job is declared. `no_overlap` (default `True`) stops a run starting while a previous run of the job is still going on any replica. A firing's record holds `status` (`completed`, `failed`, `refused` or `cancelled`), the duration, and for a failure the exception's type in `error`; the exception's text is added only when `expose_internal_errors` is set, since the record is readable by whoever can read the bucket. The lock keys carry the service's `namespace` (`cron.<namespace>.<service>.<method>.<epoch>`, without the namespace segment when there is none), so apps on one broker with the same service name do not take each other's firings. The keys are not escaped: a service named `a.b` with no namespace and a service named `b` in the namespace `a` share their keys, and only one of them runs a firing.

Semantics match `@timer`: fire-and-forget, in-process, **not** persisted — a service that is
down when a scheduled time passes does not "catch up" on the missed run when it restarts.
Cron fields are evaluated as local wall-clock times, while waits are measured as elapsed time.
Across a fall-back transition, matching times in both folds are distinct scheduled occurrences
and run at their actual instants rather than back-to-back. A time omitted by spring-forward
uses `croniter`'s next valid occurrence.
A wall clock stepped back never makes a job run an occurrence it has already started, and never makes it
run before the occurrence's time, for a distributed job as for a local one: the job waits out the rest of
the time, and the next occurrence is the first one after the last that ran.
A wall clock stepped forward past occurrences leaves them unrun on that replica, for a distributed job as for a local one (a replica of a distributed job whose clock did not step still runs them), and
the job says so: one warning when it wakes, with how many occurrences were skipped and the first and last of them.
A stop that lands while a distributed job is recording its outcome or releasing its lease lets those writes finish
before the job ends, so the lease is not left behind for `lease_ttl` and the record does not stay `running`.
Each of those writes is given up on if it has not returned in ten seconds (`DistributedCronTimer.finish_timeout`), so a broker
that goes away while the job finishes cannot hold a stop open: the failure is logged and the bucket's TTL removes what the write
would have left. A stop therefore waits at most about twenty seconds for the two writes, inside the default `shutdown_timeout` of
thirty; with a smaller `shutdown_timeout` the service's drain gives up first and reports the task, as for any task that is slow
to stop, and the writes still end within their bound.

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
- `key` — parameter name (e.g. `"order_id"`), dotted attribute path (e.g. `"order.id"`), or callable extracting a key string from `(*args, **kwargs)`. A name or path that does not resolve, or that resolves to `None`, raises `IdempotencyKeyError` when the handler is called, whether or not `hash_payload` is set; the message says which step was missing or which value was `None`.
- `hash_payload` — if `True`, computes a SHA-256 hash of the value `key` resolves to or, with no `key`, across domain payload arguments (excluding framework metadata). It never replaces a `key` that fails to resolve. Defaults to `True` when `@idempotent` is used bare without arguments.
  The hash is the same in every process, so a retry from a restarted service computes the key its first attempt did. Pydantic models, dataclasses, enums, `bytes`, sets, `datetime`, `UUID`, `Decimal` and plain JSON types all encode; a payload carrying anything else raises `IdempotencyKeyError` at publish time, naming the type and where it sat. An arbitrary object has no encoding that is the same in two processes, and a key derived from its address would deduplicate nothing while looking correct — pass `key=` instead.

Outgoing `publish_event` calls attach the extracted key as a subject-scoped `Nats-Msg-Id` header, plus the message's ordinal within the decorated call. A handler may therefore publish more than once to the same subject: each message gets its own id, and a retry of the handler reproduces them, because it republishes the same messages in the same order.

A key passed to `publish_event(idempotency_key=...)` is used as given: it carries no ordinal and does not advance the call's count, inside a decorated call or outside one. An id longer than 128 bytes is replaced by its SHA-256 hash, and an empty key counts as no key. The ordinal belongs to the key `@idempotent` derives.

That ordering is the assumption. A handler that publishes from concurrent tasks has no stable message order, so a retry of it may pair a message with a different ordinal than before — such a handler cannot be deduplicated this way, and should pass an explicit `idempotency_key=` per publish instead. The first message of a call is unsuffixed, so a handler that publishes once is unaffected.

> **Note**: `@idempotent` relies entirely on JetStream's native message deduplication window via the `Nats-Msg-Id` header. It operates without requiring `cliffracer-kv` or an external cache.

### Message Validation

Validate inbound messages against a pydantic model.

**RPC (request/reply)** — annotate the handler. A parameter annotated with a
pydantic model is validated against it and the handler receives the model;
responses use one envelope whatever the handler declared:
- success → `{"success": true, "result": ...}`
- validation failure → `{"success": false, "error": "validation failed", "details": [...field errors...]}`
- refusal / error → `{"error": "...", "timestamp": "...", "correlation_id": "..."}` (handler exceptions include `traceback`; the reply to an unknown method and the reply to a body that cannot be decoded carry the id the request's headers supplied, and `null` when they supplied none, while every other error reply carries an id, a new one when the request had none)

Every error reply also carries `code`, one of `unknown_method`, `validation_failed`, `refused`, `deadline_exceeded`, `busy` or `internal`. That is the field a client
classifies on: `error` is prose for a human, and with `expose_internal_errors`
on it is a handler's own exception text, so a crash whose message begins
`refused: ` must not be read as a refusal. A reply with no `code` comes from a
service that predates the field and is classified by its prefix, as before.

A `refused` reply carries two more fields when the refusal has them: `retry_after`, the seconds the
service asks the caller to wait (a `RetryMessage` sets it, as the rate limiter's refusal does), and
`details`, an object. A `retry_after` that is not a finite number of zero or more (`nan`, `inf`, a negative
number) is left out of the reply, as one the refusal does not carry is, since `NaN` and `Infinity` are not JSON.

An RPC handler may raise `RpcValidationError(details)` when semantic validation can happen only
after its annotated arguments have parsed, such as checking keys in an implementation-specific
configuration mapping. Request/reply callers receive `validation_failed` with those details;
fire-and-forget dispatch logs the same classification. An ordinary Pydantic exception or any other
unhandled handler exception remains `internal`.

`refused` means a check turned the caller away and the caller can act on it. An
extension that fails closed and whose hook RAISES is reported as `internal`
instead, because that is the service being broken rather than the caller being
refused, and its reply does not carry the `refused: ` prefix either -- so a
client that predates `code` reaches the same class by finding no prefix that
matches.

### Request headers and deadlines

A request carries these headers. Each is matched in any case.

| Header | Sent by | Carries |
|---|---|---|
| `Content-Type` | every caller | the body's encoding, `application/json` or `application/msgpack` |
| `X-Correlation-ID` | every caller, and every reply | the correlation id (see Correlation IDs) |
| `Cliffracer-Timeout-Ms` | `call_rpc`, `stream_rpc`, `RpcProxy`, `ServiceClient` and `cliffracer call`, on a request that waits for a reply | the whole milliseconds the caller still waits, at the moment it sends |

`Cliffracer-Timeout-Ms` is a budget, not an instant: the service turns it into a deadline on its
own clock when the request reaches dispatch, so the two hosts need not agree on the time. Only a
whole number from 1 to 86400000 (one day) is read as a budget; any other value is read as none.
The time the request spends on the wire is not counted, so a service may work up to one transit
longer than its caller waits, and never less. A fire-and-forget request (`call_async`,
`call_rpc_no_wait`) sends no budget.

The service bounds an `@rpc` handler by the earlier of that budget and its own
`max_rpc_processing_time`, counting from the moment the request arrived, its wait for a
`max_rpc_concurrency` permit included:

- A request whose deadline has passed before its handler starts, waiting for a permit or not, is
  not run.
- A handler still running at the deadline is cancelled. One that suppresses the cancellation
  and returns is still treated as cut off, and its result is not sent; one that keeps running is
  named in a warning at twice its budget.
- Either way the reply carries `code: "deadline_exceeded"`, with `budget` (the seconds given),
  `elapsed` (the seconds that had passed), and `set_by`, `"caller"` or `"service"`. A client
  raises it as `RpcDeadlineExceededError`, a `RpcTimeoutError` with those three attributes.

While a handler runs, a call it makes through `call_rpc`, `RpcProxy` or `ServiceClient` waits its
own timeout or what the handler's request has left, whichever is less, and sends that as its
budget, so a chain of calls spends one budget and stops when its first caller stops waiting. A
call made when nothing is left raises `RpcTimeoutError` and is not sent.
`cliffracer.core.deadline.current()` returns the running request's deadline (its `budget`,
`set_by` and `remaining()` seconds), or `None`. In a timer firing with a `deadline=` it is that
deadline, with `set_by` `"timer"`.

A fire-and-forget handler is bounded by `max_rpc_processing_time` alone: one that runs past it
is cancelled and logged, and one whose time runs out while it waits for a permit is dropped and
logged. With neither a budget nor `max_rpc_processing_time`, a handler runs until it returns.

### Admission and the wait for a permit

Every method's requests arrive on one subscription, and the subscription's callback does not
wait: it fixes the request's deadline, admits it, starts a task for it and returns. The task waits
for a `max_rpc_concurrency` permit (`max_async_rpc_concurrency` for a fire-and-forget request),
so a request waiting for one holds no other request, of its own method or any other.

`max_rpc_in_flight` bounds how many requests are admitted at once on each path, running or
waiting for a permit. A request over it is answered at once with code `busy`, carrying `limit`
and `in_flight`, and is not started; a fire-and-forget request over it is dropped and logged.
Unset, nothing is refused: before the callback, the NATS client holds a subscription's pending
messages up to its own limit (524288 messages or 128 MiB by default), and a burst waits there as
it always has.

A request that gets its permit while the service is stopping is answered `busy`, "not started:
the service is stopping", and its handler does not run, so the caller can try another replica. A
client raises `busy` as `RpcBusyError`, a `RpcServerError` with `limit` and `in_flight` (both
`None` for a service that was stopping).

On the caller side, `call_rpc` raises the same classes the standalone `ServiceClient`
does for the same reply, all of them `RpcError` (`RPCError`): `RpcValidationError` for a
validation failure, with the field-level errors on its `.details`; `RpcUnknownMethodError`;
`RpcRefusedError`; and `RpcServerError` for anything else an error envelope carries, with the
subject in its message. A reply is read by one rule for `call_rpc`, `cliffracer.calls.call` and a
generated client: one that cannot be decoded, that is not a JSON object, that carries no `success`
key, or whose `success` is not `true` with no `error`, is an `RpcServerError` naming the subject and
what came back. `RpcTimeoutError` is raised on timeout, `RpcNoRespondersError` when nothing
is subscribed, and `RpcConnectionError` when the connection is lost before the reply, with the
nats error as its `__cause__`; an argument larger than the broker's `max_payload` is an
`RpcClientError`, and a full outbound buffer during a reconnect, a draining connection or any other
nats-py error on the request is an `RpcConnectionError`. `call_async`, `call_rpc_no_wait`,
`publish_event` and `broadcast_message` raise the same two classes for what a connection raises on a
publish (`max_payload`, a full buffer, draining, closed, stale), so one `except RpcError` holds every send. A
JetStream error about the stream itself, such as no stream answering, reaches the caller as it is.
`RpcRefusedError` carries the reason as `.reason`, the seconds the service
asked the caller to wait as `.retry_after` (`None` when it did not) and what the refusal said besides as
`.details`. `.details` is the field-level errors for a validation error and the refusal's own object for
a refusal; it is empty for every other class:
```python
from cliffracer.core.exceptions import RPCError
try:
    await self.users.create_user(username="x")   # missing email
except RPCError as e:
    print(e.details)   # [{"loc": ["email"], "msg": "field required"}, ...]
```

**A strict model takes the JSON form of its own dump.** A payload is validated in pydantic's python
mode, and when that refuses, once more in JSON mode. A model, or a field, declared strict refuses in
python mode the forms JSON has to use: an ISO string for a `datetime`, text for a `UUID` or a
`Decimal`, an array for a tuple or a set. Those are the forms every sender writes, over JSON and
over msgpack, since cliffracer dumps to JSON values before it packs. JSON mode accepts them and still
refuses what strict is for: `"1"` for an int, `1` for a bool. A payload python mode accepts is
accepted exactly as it is, and a lax model accepts nothing it did not, with the value it always got;
neither does a msgpack producer that sends python values such as `bytes`.

When both modes refuse, the error is JSON mode's, which names the violations that are real. That
holds for a model that is not strict too, so a refusal can be worded as JSON mode words it, at the
same location:

| Field type | Python mode said | JSON mode says |
|---|---|---|
| A list, tuple, set or frozenset | "Input should be a valid list" (tuple, set, frozenset) | "Input should be a valid array" |
| A dict or a nested model | "Input should be a valid dictionary" | "Input should be an object" |
| A timedelta | "Input should be a valid timedelta" | "Input should be a valid duration" |

The error type changes in one case: a whole number of magnitude `10**18` or more given for a `date`,
a `datetime` or a `timedelta`, which python mode refuses as `date_from_datetime_parsing`,
`datetime_parsing` or `time_delta_parsing`, is refused as `date_type`, `datetime_type` or
`time_delta_type`. The message and the type reach a client in the details of its
`RpcValidationError`. A payload with python values in it, such
as `bytes` or a map whose key is not text, was not JSON to begin with: writing it as JSON would turn
those into text and accept what a strict model refuses, so it is refused with python mode's error.

One exception remains. A strict model with a `before` or `wrap` validator, on the model or on a field,
still refuses the JSON form of its own dump: that is pydantic's JSON mode, and it was refused before.

The client reads what it sends the same way the service reads it (one helper, used by both), for the
argument it checks and for the wire form it chooses. So a strict model that only its alias reads is
sent by its alias, and a dict in the JSON form of a strict model is not turned away before sending.

### RPC validation diagnostics

`ServiceConfig.rpc_validation_errors` selects RPC request diagnostics:

- `"full"` (default) preserves Pydantic details, including rejected input,
  locations, validator messages and context, plus decoder exception text.
- `"redacted"` replaces request-schema diagnostics with one fixed Pydantic
  error (`type="validation_failed"`, empty location, `msg="Invalid RPC payload"`)
  before result hooks run. Original validation exceptions are absent from
  the exposed cause/context chain. Replies and async validation logs carry
  this diagnostic. Decoder failures use fixed text; missing optional codecs
  remain internal errors. Unexpected validator exceptions remain internal
  failures with fixed text.

Choose `"redacted"` for services accepting credentials or other sensitive input.
Removing only Pydantic's `input` is insufficient: unknown keys, dictionary keys,
custom error types, messages and context can contain the same data.
`hide_input_in_errors=True` affects Pydantic's rendered text, not its structured
`errors()` or `json()` output. `expose_internal_errors` is independent and
controls internal exception disclosure; it does not select this policy.

This setting covers RPC ingestion diagnostics and a handler's deliberate `RpcValidationError`,
including fire-and-forget RPC.
It does not remove raw `WorkerContext.payload` or message bytes available to
extensions, rewrite event dead-letter records, or redact application logs,
correlation metadata, other handler exceptions, or response-schema errors. Extensions
that record payloads and clients that validate arguments locally own those
separate disclosure decisions.

**Events** — `@validated_listener(pattern, Schema, on_invalid=None)`:

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

The handler has one payload parameter annotated with a Pydantic model; its name
may describe the domain, such as `order`. The annotation must accept every
declared schema: use the schema itself, a shared Pydantic base model, or
`BaseModel` when one method handles several validated subjects. The handler may
additionally accept `subject: str` and `correlation_id: str | None`. Startup
refuses unrelated, unannotated, or scalar payload parameters, variadic
parameters, and additional payload parameters before connecting to the broker.

Invalid messages have no caller to reject to, so they are handled by `on_invalid`:
- `"deadletter"` (default): the raw payload + field-level errors are republished to the
  service's DLQ subject (`dlq.{service}`, configurable via `ServiceConfig.dlq_subject`),
  so nothing is lost and a monitor can subscribe to `dlq.*`.
- `"drop"`: the errors are logged at WARNING and the message is discarded (never silent).

Set the default for a service with `ServiceConfig(default_on_invalid="drop")`; override per
handler with `@validated_listener(..., on_invalid="deadletter")`. Both accept exactly those two
values: any other `on_invalid` raises `ConfigurationError` when the decorator runs, and any other
`default_on_invalid` fails `ServiceConfig` validation.

**Shared-schema contract (recommended):** put the pydantic model in a module both the
publisher and the consumer import, and have the publisher call `Model(**x).model_dump()`
before publishing — then most invalid messages never leave the sender.

Delivery follows the declaration, as it does for `@listener`. With `fanout=True` the
transport is core NATS: every replica receives each event once, with no redelivery, and
dead-lettering an invalid message to a subject is the pattern. With `durable="<name>"`
on a `jetstream_enabled` service the listener is a JetStream consumer: one replica
handles each event, a handler that returns acknowledges it, a handler that raises is
redelivered up to `jetstream_max_deliver` times and then dead-lettered, and a message
that fails validation is routed by `on_invalid` and terminated. The handler's entry is the
boundary: whatever raises while the payload is decoded and validated, a validator's `TypeError`
as much as pydantic's `ValidationError`, is a failure of the message and is never redelivered,
except a body in an encoding the service lacks the package to read (msgpack without the `msgpack`
extra), which is the service's fault and is redelivered like a handler failure. An exception raised
inside the handler is the handler failing and is redelivered.
A schema pydantic cannot build, such as a model with an unresolved forward reference, is refused
when the service starts, so no message is validated against one; a validator that raises while a
message is validated refuses that message, and with `on_invalid="drop"` the message is dropped.

### Describing a service, and calling one from a client

A service answers its own description on `{service}.describe`: every `@rpc`
handler, its parameters and return as structural type references, a hash per
method and one for the whole description. `cliffracer.introspect.describe(cls,
service=..., version=..., config=config)` computes the same value from the class
without a broker, so given the `ServiceConfig` the service runs with, the offline
and the live answer are the same bytes. Without `config` it leaves out what only
the configuration supplies, the declared streams and each listener's
`effective_subject`, and records the `ServiceConfig` default version unless one
is given; the methods and their hashes are the same either way. So is the hash for
the whole description. It covers the methods, each listener's subject, schema and
delivery kind, the models and the outputs, and leaves out the streams and each
listener's effective subject, durable and queue group, which the configuration
decides. It includes docstrings, which a method's `signature_hash` does not.

The description carries what the source says: each parameter's default value, each handler's
and listener's docstring, and a model's field defaults, field descriptions and docstring. It
goes to every caller the service answers `{service}.describe` for, and an extension that
refuses a message refuses this one too. A value that must not leave the service, such as a
connection string or a key, is read from configuration or the environment inside the handler
rather than written as a default, and a note for the maintainers belongs in a comment rather
than a docstring.

The Go port's description differs here. It carries no parameter defaults, so a Python description
is the one of the two that publishes them. It carries no docstrings either, so a Python
description is also the one that publishes a handler's and a model's documentation.

Each parameter's `type` and each method's `returns` is a TypeRef, a JSON object whose `kind` says
what the rest of it holds. `cliffracer-generate-client` writes each back as an annotation:

| kind | fields | the generated client writes | the hashes see |
|---|---|---|---|
| `"scalar"` | `name`: `str`, `int`, `float`, `bool` or `none` | that type, `None` for `none` | the name |
| `"model"` | `module`, `qualname`, `schema_hash`: the first 16 hex digits of the SHA-256 of the model's JSON Schema, as a caller sends it for a parameter and as the handler writes it for a return | the model, imported from `module` (a nested `Outer.Inner` imports `Outer`) | the module, the qualname and `schema_hash`, so a change to the model's schema changes the method's hash |
| `"list"` | `item`: a TypeRef | `list[item]` | the item |
| `"dict"` | `value`: a TypeRef; the keys are always `str` | `dict[str, value]` | the value |
| `"optional"` | `inner`: a TypeRef, from `T \| None` or `Optional[T]` | `inner \| None` | the inner type |
| `"literal"` | `values`: `str`, `int` or `bool` values, an enum member as its value | `Literal[...]` | the values, in order |
| `"stream"` | `item`: a TypeRef; a method's `returns` only | `AsyncIterator[item]`, and the method is iterated | the item |

Any kind may also carry `constraints`, from `Annotated` metadata: `ge`, `le`, `gt`, `lt`,
`min_length`, `max_length`, `pattern`, `strict`, `multiple_of`, `max_digits`, `decimal_places`,
`allow_inf_nan` and `coerce_numbers_to_str`, sorted by name. A method's `signature_hash` is the
hash of its parameters (each one's name, TypeRef and default) and its `returns` TypeRef, so every
field above, the constraints included, changes it.

Any other annotation is refused when the service starts, and by `describe`, naming the type: `Any`,
a bare `list`, `dict`, `set` or `tuple`, a `dict` whose keys are not `str`, and a union of two
types that are not `None`.

A `stream` is the return of a handler that streams its reply, an async generator (see
[A handler that streams its reply](#a-handler-that-streams-its-reply)), and a generated client's
method for it is an async generator too, read with `async for`. An `AsyncIterator` return on a
handler that is not one is refused when the service starts.

`ServiceClient` is the base a typed client extends. It encodes each argument
through the annotation the method declares and sends the request as JSON,
labelled `Content-Type: application/json`, validates the reply (decoded by the
encoding its `Content-Type` names) against the declared return type, and maps
the reply envelope to an exception naming which
side is at fault. `RpcValidationError` (with pydantic's `details`),
`RpcUnknownMethod`, `RpcRefused`, `RpcNoResponders` and `RpcTimeout` are
`RpcClientError`s: things the caller can act on by fixing its arguments,
calling a method that exists, or retrying. Anything else the service reports is
an `RpcServerError` -- the `Internal server error` envelope a handler's
unhandled exception produces, and a reply that omits `success`. The two are
disjoint, so `except RpcClientError` does not swallow a remote fault and
`except RpcServerError` catches one; `except RpcError` catches both. On the
first call the client compares its per-method hashes against the running
service and raises `ClientOutOfDate`, naming the methods that moved. That
comparison happens once per connection however many calls race for it:
concurrent first calls share one describe rather than each sending their own,
so they cannot sample different replicas of a rolling deploy and disagree.
Calling `verify()` yourself always asks.

A model argument goes out in the form the service's validation reads back as that
argument. Every path offers the same forms: the model's default dump, the dump by alias, the
dump by field name, and the forms written a level at a time. A model read only by its alias is sent by alias, one read by field name is
sent by field name, a field read through `AliasChoices` or `AliasPath` is written under
the first choice or into the structure the path names, and a tree whose levels need different forms (an outer model read
by field name that holds an inner model read by alias) is written one model at a time.
`call_rpc`, `call_async`, `call_rpc_no_wait` and `RpcProxy` do the same, taking the
argument's own class, and the base classes that tell the two forms apart, for the
service's model. A generated client sends the validation-alias form only where the declared
annotation reads it back as the argument (or as its validators make of the caller's values), and
otherwise chooses among the other forms as above. `call_rpc` and the rest withhold it where a
model class of the argument's hierarchy would read it as other values or reads one of the other
forms instead, so each class a handler may declare either reads it as the argument or refuses it. Three limits. A subclass read by field name whose base reads only by
alias is sent by alias, which the base reads, and a handler that declares the subclass
itself refuses it; the mirror case, a subclass whose own class reads the alias form sent to
a handler that declares a base read only by field name, is refused the same way. A tree that needs two forms is written whole when any model on the way
declares a `field_serializer`, a `model_serializer` or a `computed_field`, carries
extra fields, or holds a serializer written in `Annotated` that cannot write the model one
level at a time, and the service then refuses it; `populate_by_name=True` on the model that
is read by alias lets the whole tree be written in one form. And a model whose aliases
are other fields' names (`b` is read from `c`, which is also a field) has no dict that
reads back as the argument; when its fields hold different values it is refused as below.

When no form reads back as the argument, the first one the service accepts is sent: a model whose
validator normalises a value reads back changed in every form, and goes out as it always did. But
when that form would make the service read a field the caller set as anything other than what the
model's own validators make of the caller's value, or of the model's own dump of it, the value
would be lost or changed, not normalised, so the client raises `RpcValidationError` before sending,
naming each such field in `details`: a `value_would_be_lost` entry for a field read as its default,
a `value_would_be_misread` entry for any other value, whose message names another field when the
value is that field's. A NaN read back is the NaN sent. A value the model's serializer writes is
taken as the field's value only when it writes it alike by alias and by field name: a serializer
that writes a field one way by alias and another by field name says nothing about what the
receiver should hold, and the field is judged against the caller's value. An `AliasChoices` whose first member is another field's
name, an `AliasPath` whose head is another field's name, and a chain of aliases each naming the
next field, are refused this way when the fields hold different values. `call_rpc` and the rest
refuse so only when no model class of the argument's hierarchy reads the form. A form the
service's class refuses is not refused by the client, since the receiver may be another version of
the class that accepts it: it is sent, and the service refuses the call. So a value the declared
class itself refuses, which only bypassing its validation can produce (a `Decimal` NaN or infinity
held via `model_construct`), goes out as dumped (`"NaN"`) and is refused by the service's
validator, on every path.

"Reads back as the argument" compares values, so a value can arrive as another class its declared
union allows when it equals what was set. A `str`-mixin enum or `StrEnum` member at `Colour | str`,
or an `IntEnum` member at `Level | int`, is written as its value, and pydantic's smart union reads
that value as the plain `str` or `int`. That equals the member, so it is sent, and the handler
receives `"red"`, not `Colour.RED`. A generated client, `call_rpc` and the rest, `publish_event` and
`broadcast_message` all behave this way, and no form can carry the member, since its value is
exactly a `str` or an `int`. To receive the member, declare the field as the enum alone, or as
`Annotated[Colour | str, Field(union_mode="left_to_right")]`, which tries the enum first and still
reads any other text as `str`. A plain `Enum` member at `Colour | str` reads back as another value
(`Colour.RED != "red"`), so it is refused as above. Likewise, a `list` or `dict` subclass a model
holds past validation (built with `model_construct`, or assigned) arrives as a plain `list` or
`dict`. Validation makes that change too. A model subclass instance sent where its base is
declared, as the argument or held in a field, a list, a tuple or a dict, keeps the fields only the
subclass declares when the base has `extra="allow"`: they are written beside the base's fields and
the receiver holds them as its extras. A base that ignores or forbids extras could not hold them,
so for it they are not on the wire, as pydantic writes a model by its declared class.

A request carries the correlation id of the trace it belongs to: one the
caller set explicitly, else the ambient `CorrelationContext` id, else a new
one. So a service calling a peer through a generated client continues the
trace of the request it is handling, which is what `call_rpc` and
`publish_event` have always done.

The client opens its connection on first use. `connect_timeout` (default
`30.0`, matching `ServiceConfig`) bounds that first dial and `None` disables the
bound; a dial that exceeds it, or a broker that cannot be reached at all,
raises `RpcConnectionError`. A dial that fails is the caller's to act on -- a
wrong address, a broker that is not up -- so it is an `RpcClientError` like the
others above, and `except RpcClientError` and `except RpcError` both catch it.
Once connected the client reconnects indefinitely, as ADR-0008 specifies, and a connection
nats-py closes for good (an authentication change, or a terminal error from the server) is dialled
again by the next call, with the drift check run again; a connection the client was handed is its
owner's to replace. `timeout` (default `30.0`) is separate and bounds each request, not the dial.
Both must be positive, finite numbers of seconds (`connect_timeout` may also be `None`); anything
else is refused when the client is built, with a `ValueError` naming the argument.
A dial that times out after the broker reported an error, an authorization failure for instance,
says what that error was. A service, namespace or subject prefix that cannot be part of a subject
(the rules `ServiceConfig` applies to them) raises `ValueError` when the client is built; the prefix from `CLIFFRACER_SUBJECT_PREFIX` is checked only when the client is given none. A
correlation ID in the client's `headers` is read in any case and under any of the names the service
reads, and is the one every request carries.

`RpcTimeout` is also a real `TimeoutError` -- the builtin, which is
`asyncio.TimeoutError` on 3.11+ -- so `except TimeoutError:` around a call
catches it, as it did before the client wrapped the broker's own timeout.

Every failure on the request path arrives as one of those classes. A reply that
cannot be decoded, is not a JSON object, or carries a `result` that does not
match the declared return type is an `RpcServerError` naming the subject and
showing what came back; an argument this client cannot encode is an
`RpcClientError`, raised before anything is sent. Every nats-py error on the request path is an
`RpcError` with the nats-py error as its `__cause__`: an argument larger than the broker's
`max_payload` is an `RpcClientError`, since no connection can send it, and a full outbound buffer
during a reconnect, a draining connection or any other nats-py error is an `RpcConnectionError`.
`verify()` reads its reply the
same way a call does, so a `{service}.describe` answered by something that is
not a Cliffracer service reports the subject and points at `service=` and
`namespace=` rather than raising `KeyError`.

`close()` drains a connection the client opened and refuses any later use
of that client: a call afterwards raises `RpcConnectionError` naming it, rather
than reopening. While the broker is away the connection cannot be drained, so it is closed
instead and nothing is raised; a `close()` that runs during the first connect closes the
connection that connect returns. Closing is an intent to stop, so construct a new client if you
need one. A client given a connection it did not open is not affected -- that
connection is left alone and the client keeps working, since ending someone
else's connection was never this client's to do. A connection lost underneath a
call in flight raises `RpcConnectionError` too, naming the subject.

```python
from cliffracer import ServiceClient

class OrdersClient(ServiceClient):
    SERVICE = "orders"
    SIGNATURES = {"create": "sha256:..."}

    async def create(self, order: Order) -> Receipt:
        return await self._call("create", {"order": self._encode(order, Order)}, Receipt)


async with OrdersClient(nats_url="nats://localhost:4222") as orders:
    receipt = await orders.create(Order(sku="a", qty=1))
```

A client that opened its own connection should be closed, and `async with` is
how: entering connects, so an unreachable broker is reported at that line rather
than at the first call, and leaving releases the connection however the block
ended, and the block's own exception is the one that propagates. A client given a connection it did not open is unaffected -- that
connection is left alone and the client keeps working, the same rule `close()`
follows.

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
produce the same bytes when `--version` is the version the service's
`ServiceConfig` declares. A class cannot see the config it is started with, so
`--class` records `--version`, or the `ServiceConfig` default without it;
without `--class` the running service reports its own version and `--version` is
refused. `--header` is repeatable and is what a service behind `AuthExtension`
needs, since it refuses an unauthenticated describe like any other message. A broker
that confines the client role to its own inbox prefix (see
[broker permissions](broker-permissions.md)) needs `--inbox-prefix` with that prefix; the
broker's user and password or token go in the `--nats-url`. A broker that refuses the
credentials, or a permission the request needs, is exit 3 with the broker's reason in the
message, not a missing broker or a missing service.
Without `--out` the client goes to stdout. `--namespace` names the namespace the
service runs in: without `--class` the describe request is sent inside it, and
in both forms the client records it as `NAMESPACE`, which a client constructed
without `namespace=` calls in. Pass `namespace=""` to call a client generated
with a namespace outside it. A namespace that is not a single subject token
exits 7, as `ServiceConfig` would refuse it.

A parameter whose default is a model, or a list or dict of models, gets that default built:
`item: Item = Item.model_validate({...}, strict=False)`, and each member of a list or dict the same
way. It type-checks against the annotation and `inspect.signature` shows an `Item`. The instance is
built once, when the client module is imported, and every call that leaves the argument out passes
that one object, which the client only reads. Each call and each list or dict default carries
`# noqa: B008` or `# noqa: B006` on the line that opens it. The value is the service's own JSON
dump, in which a datetime is a string and a set a list, and `strict=False` makes the model accept
that form even when the model, or one of its fields, is declared strict.

The service decides whether a default can be built. For each parameter that has a default and holds
a model, its description carries `rebuildable`, true only when the service has validated the default's
own dump the way the client would, with the models it runs, and got a value equal to its own that
dumps to the same JSON values. Where it is not true the default stays the dict the service described,
and a call that leaves the argument out sends it as it always did. A description without the key,
from a service that predates it, is read as not rebuildable. It stays a dict for:

- aliases that do not survive the dump, such as two fields whose aliases are each other's names;
- a union whose JSON form fits more than one member, such as a `date | datetime` at midnight or a
  `float | Decimal`;
- a serializer that changes a field's type or is not idempotent, a validator that is not idempotent,
  `Json[...]` fields, bytes dumped as base64 and validated as text, and a secret, which dumps masked.

One thing the service cannot see remains: the importing project's copy of the model. The check
runs on the service's models, so a client whose copy refuses the default, because it has a new
required field or a constraint the service's copy lacks, fails the import with pydantic's
`ValidationError`.


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

| exit | meaning |
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

Exit 4 covers a handler that is not annotated, a model whose module a generated
client could not import, such as one defined in `__main__`, and a class or
running service with no `@rpc` handler, whose client would have nothing to call.
A handler whose name starts with an underscore is not published (a service
with one refuses to start, naming it), so a class whose only handlers are
underscored has none. It also covers a reply to
`describe` that is not a description -- not JSON at all, a missing key, a value
of the wrong kind, a type the generator does not know -- and the message names
where the reply went wrong, or shows the start of a reply that is not JSON. Exit
3 is only for a broker the command could not use: a reply of any kind means one
answered. A handler cannot be named after anything a `ServiceClient` already
has, a method such as `verify` or an attribute such as `connect_timeout`,
because the generated method would replace it or be shadowed by it; a service
with one refuses to start, and a reply that names one exits 4. Exit 5 covers a
`--class` target that is a function, a module or any other object rather than a
class. No failure writes a file, and that holds for the write itself: the source
goes to a temporary file beside the target and is moved onto it only once whole,
so a failed write leaves the previous client exactly as it was rather than
truncating it. Exit 6 is that failure -- a missing directory, a read-only path,
a full disk. Exit 7 is a mistake in the invocation itself, argparse's usage
errors included, and not argparse's usual 2, which already means a broker
answered: a `--header` that is not `NAME=VALUE`, `--version` without `--class`,
or `--header`, `--nats-url`, `--timeout` or `--inbox-prefix` with it.

### Message base classes

`Message`, `RPCRequest`, `RPCResponse` and `BroadcastMessage` are optional pydantic base
classes for your own models. Nothing requires them: any pydantic model works as an RPC
parameter, an RPC result or an event payload.

- `Message` adds `timestamp`, set when the model is built, and `correlation_id`, filled from
  the request's when left `None` (see Correlation IDs).
- `RPCRequest` is a `Message` and adds nothing.
- `RPCResponse` adds `success`, `error` and `details`. They are ordinary fields of your result:
  a handler's return value travels in the reply envelope's `result`, so an `RPCResponse` carries
  its own `success` inside the envelope's `success`, and the framework reads only the
  envelope's. A handler reports failure by raising, never by setting `RPCResponse.success`.
- `BroadcastMessage` adds `source_service`.

They ignore a field they do not declare, as pydantic does by default; the model the framework
builds from a handler's annotations refuses one.

### Correlation IDs

A correlation ID is tracked per request via a `contextvars.ContextVar` and propagated
two ways: in the JSON body **and** the NATS message headers of every `call_rpc`,
`call_async`, and `publish_event` (receivers read the headers first, the body as a fallback —
so the ID survives even a non-Cliffracer consumer). Because it's a context variable, it
also flows automatically into `asyncio.create_task(...)` spawned inside a handler (Python
copies the context at task creation). The one exception is work pushed to a **thread**
(`run_in_executor`) — context variables do not cross threads, so capture and re-set the ID
manually there.

A `Message` model (`RPCRequest`, `RPCResponse`, `BroadcastMessage` and their subclasses)
declares a `correlation_id` field. When the framework builds one for a handler -- an RPC
argument, a listener's message -- or takes one back as an RPC result, a field left as
`None` is filled with the request's correlation ID, so a reply's `result` carries the same
ID as the envelope around it. A value set explicitly is kept; the envelope's ID is the one
the framework propagates. Other models are left alone, even if they declare a field of that
name.

### The event envelope

`publish_event` and `broadcast_message` wrap what they send in an envelope with
two levels, and the levels answer different questions:

```json
{
  "source_service": "relay",
  "timestamp": "2026-09-16T18:19:29.769188+00:00",
  "correlation_id": "corr_6a8287a289cf431e",
  "data": {
    "notification_id": "n1",
    "source_service": "upstream-producer",
    "timestamp": "2020-01-01T00:00:00Z"
  }
}
```

- The top level describes **this publish**: `source_service` is the service that
  sent this message and `timestamp` is when it did.
- `data` is the payload exactly as the publisher was given it. A
  `BroadcastMessage` carries its own `source_service` and `timestamp`, and they
  arrive here unchanged.

A listener is handed `data`, whether it is a `@listener` taking keyword
arguments or a `@validated_listener` taking a model. So a service that receives
a broadcast and publishes it again, as above, does not overwrite the producer:
its own name is on the envelope, and every listener still reads
`upstream-producer`.

The top level holds only those envelope keys. A reader of the raw message
finds the payload's fields under `data`, never beside the envelope's. A
`correlation_id` passed to either publisher is not payload at all; it becomes
the envelope's `correlation_id` and is sent in the headers.

A model in `data`, at any depth inside dicts, lists, tuples and sets, is written as `call_rpc`
writes an argument: in the form its own class, or a base class that tells the forms apart, reads
back as itself (by alias first, then by field name, then a level at a time), with the
validation-alias form only where no model class of its hierarchy would read it as other values.
When no model class of its hierarchy reads the form that would be sent, and its own class would
read a field the caller set as anything other than what its validators make of the caller's value,
`publish_event` and `broadcast_message` raise `RpcValidationError` before publishing, naming each field
(`value_would_be_lost`, `value_would_be_misread`). The idempotency key and what a send hook is
handed are computed from the payload as passed, not from the written form.

### Publishing an event at a later time

`service.schedules` publishes an event that the broker writes onto its subject later, through
JetStream message schedules. It needs **nats-server 2.12 or later**, `jetstream_enabled`, and one
declared stream with `allow_msg_schedules=True` that covers both the subject and its schedule
subject.

```python
from datetime import timedelta

from cliffracer import ServiceConfig, StreamSpec

config = ServiceConfig(
    name="orders",
    jetstream_enabled=True,
    jetstream_streams=[
        StreamSpec(
            name="REMINDERS",
            subjects=["reminders.>", "_sched.reminders.>"],
            allow_msg_schedules=True,
        ),
        StreamSpec(name="DLQ", subjects=["dlq.>"]),
    ],
)

# Inside a handler of a service built with this config:
#     await self.schedules.publish_in("reminders.due", after=timedelta(hours=1), key="order-42", order_id="42")
#     await self.schedules.cancel("reminders.due", key="order-42")
```

- `publish_at(subject, *, when, key, **kwargs)` publishes the event, built as `publish_event`
  builds it, to the schedule subject `_sched.<subject>.<key>`, namespaced and prefixed as
  `subject` is. The broker writes it onto `subject` at `when`, an aware datetime; a naive one
  raises `TypeError`. A `when` already past is written at once.
- `publish_in(subject, *, after, key, **kwargs)` is `publish_at` with `when` now plus `after`, a
  `timedelta` of zero or more, read from this host's clock.
- `cancel(subject, *, key)` removes the schedule if it has not fired. Nothing is written.
- `key` is one subject token (no `.`, `*`, `>` or white space). It names the schedule: publishing
  again under the same key replaces it, and `cancel` names it.

The schedule is a stream message, so it survives a restart of every service and of the broker,
and a schedule that comes due while the broker is down is written when it is back. No service
runs a timer for it.

**What a listener receives.** The broker writes the event onto `subject` as a stream message, so a
durable listener reads it. A core subscription does not see it. Delivery is at least once, as for
any stream message. The written copy keeps the event's `Content-Type` and `X-Correlation-ID` and
adds `Nats-Scheduler` (the schedule subject) and `Nats-Schedule-Next`. It does not keep a
`Nats-Msg-Id`: an `idempotency_key` deduplicates the scheduling publish, not the firing. A handler
that must act once keys on `Nats-Scheduler`, which is the same on every redelivery.

**Refusals.** `MessageScheduleError`, a `StreamDeclarationError`, says which of these is missing:
- a declared stream covering the subject or the schedule subject;
- the two in one stream;
- `allow_msg_schedules` on that stream;
- a broker of 2.12 or later.

The first three are refused before anything is sent. When the server itself refuses a schedule,
its `err_code` (10188, 10189, 10190) is in `details`. Without JetStream the call raises
`ConfigurationError`.

A schedule fires once. Recurring work is `@timer`, or `@cron` from `cliffracer-cron`.

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

A proxy is declared as a class attribute. Assigning to its name on an instance (`self.other =
...`) is refused with an `AttributeError`, since the value would hide the proxy for that one
instance; to substitute one, replace the attribute on the class.

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
async def on_order(self, subject: str, order_id: str) -> None:
    ...
```

`*` matches exactly one token, so the subscription matches `<namespace>.orders.created` for any
namespace and never a publisher that has no namespace and publishes plain `orders.created`.
`cross_namespace=True` therefore needs the service to have a `namespace`: on a service with none,
discovery refuses the listener with a `ConfigurationError`.

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
| `nats_url` | `'nats://localhost:4222'` | Broker to connect to: one server, as `nats://`, `tls://`, `ws://` or `wss://` (lower case), or a bare `host` or `host:port`. A value nats-py cannot connect to is refused when the config is built, naming the reason, and the refusal never repeats a password in the URL. A password embedded in the URL is not printed either: the config's `repr`, a printed `model_dump()` and `__dict__` show the URL without it, `model_dump_json()` carries the URL with it withheld, and `config.nats_url` is the whole URL as a `str`. Prefer the auth fields below to embedding credentials here. |
| `nats_user` | `None` | Username, if the broker requires user/password auth. |
| `nats_password` | `None` | Password for `nats_user`. Set both or neither. Held as a `SecretStr`: the config's repr, its dumps and its validation errors show it masked or not at all, and `config.nats_password.get_secret_value()` reads it. |
| `nats_token` | `None` | Token auth, held as a `SecretStr` like the password, as an alternative to user/password: a config that names more than one way to authenticate (user and password, a token, a credentials file) is refused when it is built, and so is a user without a password or the reverse. |
| `nats_credentials_file` | `None` | Path to a NATS `.creds` file, for NGS or an operator-mode broker. |
| `nats_inbox_prefix` | `None` | Dedicated request and delivery inbox prefix for this broker role. |
| `max_reconnect_attempts` | `-1` | How many times to retry a lost connection. `-1` retries forever, and so does `0` in nats-py; a finite value ends in a permanent close, which is what `exit_on_closed` then decides about. |
| `reconnect_time_wait` | `2` | Seconds between reconnect attempts. |
| `ping_interval` | `None` | Seconds between the pings that check a quiet connection is still answering. Unset leaves nats-py's default of 120. A silent partition is noticed between `max_outstanding_pings * ping_interval` and `(max_outstanding_pings + 1) * ping_interval` seconds after it begins. |
| `max_outstanding_pings` | `None` | Pings that may go unanswered before the connection is treated as lost and reconnection starts. Unset leaves nats-py's default of 2. At least 1: zero would call a healthy connection lost at its first tick. |
| `broker_probe_timeout` | `2.0` | Seconds the readiness check waits for the broker to answer a round trip. A round trip that fails or takes longer makes the status `disconnected`. `None` turns the probe off, and readiness reads nats-py's connection flag alone. |
| `broker_probe_cache` | `1.0` | Seconds the readiness check reuses the last round trip's result, a failure included, so a burst of probes costs one round trip. `0` probes on every check. |
| `dependency_probe_interval` | `5.0` | Seconds between background probes of the dependencies a listener names in `pause_when_down`. No background probe runs when no listener names one. |
| `dependency_pause_after` | `2` | Consecutive failed background probes after which a dependency counts as down and the listeners that name it in `pause_when_down` stop consuming. |
| `dependency_resume_after` | `2` | Consecutive passing background probes after which a dependency counts as up again and a listener paused on it resumes, once every dependency it names is up. |
| `connect_timeout` | `30.0` | Seconds the FIRST connect may take before the service gives up, logs the address and raises `NatsError`. `None` disables the timeout: `max_reconnect_attempts` governs the first connect, and `-1` and `0` both mean forever. Reconnects are unaffected. |
| `shutdown_timeout` | `30.0` | Seconds a timer's run in flight is given to finish (all timers at once, first), then seconds to drain active tasks, followed by an equal cancellation grace, and then the connection's drain, bounded by the same number of seconds. `None` waits without a deadline. A value at or below zero, which this field refuses, can reach a stop only around validation; there it is bounded at 30 seconds and logged as a warning. A stop begun because the broker connection closed for good is cut off after 10 seconds whatever this is set to; its `on_shutdown` still runs after the cut-off, for up to this many seconds (30 when this is `None`). |
| `exit_on_closed` | `True` | On a permanent close, log at ERROR and initiate graceful shutdown (`await self.stop()`). Set `False` to keep the service running and poll `status: "disconnected"` on the health endpoint. |
| `request_timeout` | `30.0` | Seconds an outbound RPC waits for its reply before raising, or what the request a handler is answering has left if that is less. The call sends what it waits as `Cliffracer-Timeout-Ms`. |
| `serialization_format` | `'json'` | Payload serialization format: `'json'` or `'msgpack'`. `'msgpack'` needs the `cliffracer[msgpack]` extra, and a service configured for it without the package is refused when it starts, with a `ConfigurationError` that says how to install it. A service answers in the encoding the REQUEST asks for, whatever this is set to, so a caller can send msgpack to a service configured for JSON; if the extra is not installed the service answers in JSON with `unsupported serialization format`, which a client raises as `RpcServerError`. |
| `expose_internal_errors` | `False` | Whether an exception's own text may leave the process. Governs wire RPC error responses (with a traceback), the reply to a `{service}.describe` that fails, the reason a caller is given when an extension that fails closed raises from its own check, and the health endpoint's error strings — a failing dependency probe, an extension's `health_details`, a failure in the extension loop itself, the dependency sweep and the catch-all 500 — and the `error` of a distributed cron firing's record in its KV bucket, and the `error` of the dead letter of a handler that failed on its last delivery. Unset, each reports a fixed string and the detail stays in the log (the cron record and the dead letter hold the exception's type); the status, the `ok` flags, the failing dependency names and the name of the extension that refused are reported either way. A refusal a check authored itself — `refused: unauthenticated` — is not an exception's text and is delivered whole regardless. |
| `rpc_validation_errors` | `'full'` | RPC request validation diagnostics: full preserves Pydantic details; redacted uses fixed diagnostics in replies, async logs, and validation exception chains. Does not redact raw payloads, event DLQs, or handler/response errors. |
| `max_rpc_concurrency` | `None` | Maximum concurrent RPC handlers executing simultaneously. |
| `max_event_concurrency` | `None` | Maximum concurrent event handlers executing simultaneously, on push and pull listeners alike. A pull consumer's fetched batch is dispatched concurrently and this is what bounds it; `None` leaves the batch size and `jetstream_max_ack_pending` as the only bounds. A JetStream message that waits for a permit is sent in-progress pulses while it waits, so the server does not redeliver it; a service that is stopping starts none of them, and the broker redelivers them. |
| `max_async_rpc_concurrency` | `None` | Maximum concurrent async fire-and-forget RPC handlers executing simultaneously. |
| `max_rpc_in_flight` | `None` | Most RPC requests admitted at once on each of the request-reply and fire-and-forget paths: running, or waiting for a concurrency permit. A request over it is answered with code `busy` (a fire-and-forget one is dropped and logged) and not started. Unset: no bound; before the delivery callback, nats-py's per-subscription pending limit (524288 messages or 128 MiB by default) still applies. See Admission and the wait for a permit in the API reference. |
| `max_stream_items` | `None` | Items a handler that streams its reply may send in one stream. The item that would pass it is not sent: the stream ends with code `refused`, naming the limit and the items sent. None sets no limit. |
| `max_stream_bytes` | `None` | Bytes of item bodies a handler that streams its reply may send in one stream. The item that would pass it is not sent: the stream ends with code `refused`, naming the limit and the items sent. None sets no limit. |
| `max_rpc_processing_time` | `None` | Seconds an RPC handler may run, including its wait for a concurrency permit, before it is cancelled. A request-reply handler is also bounded by the budget its caller sends in `Cliffracer-Timeout-Ms`, whichever ends first, and is answered with code `deadline_exceeded`; a fire-and-forget handler is bounded by this alone and logged. None sets no bound of the service's own. See Request headers and deadlines in the API reference. |
| `jetstream_enabled` | `False` | Enable JetStream. Every `jetstream_*` field below is ignored while this is `False`. |
| `jetstream_resource_mode` | `'provision'` | `'provision'` lists streams and creates missing declared streams and durable consumers. `'bind'` reads each declared stream and consumer by name, validates its contract, and binds without resource creation or account-wide LIST/NAMES access. |
| `jetstream_streams` | `[]` | Streams this service declares at startup, as `StreamSpec` entries. A durable listener whose subject no stream here covers fails startup, and so does a stream claiming a subject another stream already claims — see [Stream subjects are exclusive](#stream-subjects-are-exclusive). |
| `jetstream_max_deliver` | `5` | Redelivery limit before a message goes to the DLQ. Write-once per durable: the server keeps the value the consumer was created with. A failing message is dead-lettered at the lower of the two, read once at subscribe, and the DLQ record's `delivery_limit` says which decided (`"server max_deliver 3"` or `"config jetstream_max_deliver 5"`). |
| `jetstream_ack_wait` | `30.0` | Seconds the server waits for an ack before redelivering. Write-once per durable. The heartbeat that keeps a running handler's message alive pulses at half the shorter of this and the `ack_wait` the durable already has on the server, which the service reads when it subscribes and warns about when they differ. |
| `jetstream_max_ack_pending` | `64` | In-flight bound: unacked messages the server will hand this service at once. Write-once per durable. |
| `jetstream_pull_batch` | `8` | How many messages a pull consumer fetches per request. Small on purpose; `jetstream_max_ack_pending` is what bounds in-flight work. |
| `jetstream_pull_timeout` | `5.0` | Seconds a pull fetch waits before returning empty. |
| `max_processing_time` | `None` | Seconds one replica may spend on a JetStream message before the handler is cancelled and the message redelivered. None leaves it unbounded, which is what the heartbeat does today. |
| `jetstream_nak_backoff` | `1.0` | Base seconds before a naked message is redelivered. Doubles per delivery. A pull consumer whose fetch fails waits the same base, one second at least, before it fetches again, and doubles that wait for each failure in a row. |
| `jetstream_max_backoff` | `60.0` | Ceiling for the doubling `jetstream_nak_backoff`, and for the wait after failed fetches, which is never under one second. |
| `jetstream_update_streams` | `False` | Let startup update the fields declared by `StreamSpec`: subjects, storage, retention, maximum age, duplicate window, message schedules when the declaration allows them, and the limits, discard policy and replicas it declares. A limit lowered below what the stream holds is refused rather than applied. Limits, discard policy and replicas it leaves out, placement, description, metadata and fields outside `StreamSpec` are preserved. Off by default: two services declaring the same stream differently would flap it on every boot. |
| `idempotent_publishing` | `False` | Whether to automatically generate idempotency keys from domain payloads. |
| `on_connect` | `None` | Called after the initial connection and again after every reconnection, so it must be safe to run more than once. Setup that must happen once belongs in `on_startup`. |
| `on_disconnect` | `None` | Called when the connection drops, before reconnection is attempted. |
| `on_error` | `None` | Called with the exception for connection-level errors nats-py reports. |
| `version` | `'0.1.0'` | Reported on the health endpoint and in service metadata. |
| `health_listener` | `True` | Serve the built-in health endpoint. Off means nothing listens and container healthchecks reading it will fail; see `CliffracerService.health_listener` for what it serves. |
| `health_host` | `'127.0.0.1'` | Interface the health endpoint binds. A container healthcheck runs in the container's own network namespace, so loopback is enough for it. Set `'0.0.0.0'` when something outside the container reads `/health` -- a reverse proxy, or a probe on another host. A value that is not an IP address or a host name is refused when the config is built; a name that merely does not resolve is not, since that is for the bind to report. |
| `health_port` | `8000` | Port for the health endpoint. A port already in use raises at startup, so two services in one process need different values here. `0` asks the operating system for a free port; `/info` and the startup log report the one it bound. |
| `description` | `None` | Free text, reported in service metadata. |
| `namespace` | `None` | Prefix token for subject isolation between apps on a shared broker. A single subject token: no `.`, `*`, `>` or whitespace. |
| `subject_prefix` | `None` | Outermost prefix for every subject, stream and durable consumer this service touches, separating one environment's or tenant's traffic from another's on a shared broker. Applied outside `namespace`, so a `cross_namespace` listener resolves to `<subject_prefix>.*.<pattern>` and still reads only its own environment. Letters, digits and underscores only: it also names streams and durables, which take no `.`. Defaults from `$CLIFFRACER_SUBJECT_PREFIX`. |
| `auto_restart` | `True` | Let `ServiceOrchestrator` restart this service if it stops. |
| `restart_delay` | `1.0` | Seconds the orchestrator waits before restarting. |
| `default_on_invalid` | `'deadletter'` | What to do with a message that fails validation: `deadletter` publishes it to `dlq_subject`, `drop` discards it. A handler can override per listener. |
| `dlq_subject` | `'dlq.{service}'` | Where dead-lettered messages go. `{service}` is substituted with `name`. [Dead letters](dead-letters.md) says what is published there and how to read it. |
<!-- /service-config-fields -->

### Tasks that refuse shutdown

A timer or cron job that is mid-run when the service stops is given up to `shutdown_timeout`
seconds to finish before it is cancelled, all timers at once and before the drain below: the
timer stops scheduling at once, and a timer only waiting for its next firing is stopped at
once. A run that is cancelled and does not finish, because it catches the cancellation and
carries on, is handed to the drain below and treated as any task that refuses to stop. Worst
case a shutdown therefore takes three `shutdown_timeout` periods (timers, drain, cancellation
grace), not two, and then the connection's own drain, which is given up to one more
`shutdown_timeout`: against a broker that has gone silent the drain waits on an answer that never
comes, and when its period ends the connection is closed anyway and a warning says messages still
buffered may not have been sent. An extension's `stop()` has no deadline of its own.
A timer stopped on its own with `Timer.stop()` gives such a run `cancel_grace`
seconds (by default the service's `shutdown_timeout`, or 30 seconds for a timer that belongs to
no service), then reports it at error level and returns.

Shutdown gives in-flight tasks `shutdown_timeout` seconds to finish, then logs
and cancels unfinished tasks. Cancellation cleanup shares one further
`shutdown_timeout` budget, including supervised work spawned during cleanup.
Tasks still running are named at error level and remain in `active_tasks` until
they finish. A stopped service cannot restart while these tasks remain active. Runners also
refuse to replace it with a new instance on the same loop; they report the
service down so a process supervisor can restart it.
Shutdown does not mark their work successful or guarantee that side effects or
message acknowledgements cannot occur later.

`CliffracerService.run()`, `ServiceRunner.run_forever()` and
`ServiceOrchestrator.run_forever()` own their event loop and close it without
joining these tasks again. Other leftover tasks receive one cancellation grace,
using the service's configured timeout (the longest timeout for an orchestrator;
any `None` permits an unlimited wait). This bounds asynchronous task draining,
not shutdown hooks, asynchronous generator cleanup, blocking code or executor
threads.

A handler that calls `stop()` itself, such as a "shutdown" RPC, is not waited for by the drain: its
task is left alone while every other task is drained and cancelled, and `await self.stop()` returns.
But the stop disconnects the service, so the handler finishes and cannot send its reply: the caller's
request times out. A shutdown handler should start the stop and return, so the reply goes out first:

```python
import asyncio

from cliffracer import CliffracerService, rpc


class Controlled(CliffracerService):
    @rpc
    async def shutdown(self) -> dict[str, bool]:
        self._stopping = asyncio.create_task(self.stop())
        return {"stopping": True}
```

Keep a reference to the task, as `self._stopping` does: the event loop holds a task only weakly, so
a task nothing refers to can be garbage-collected before it finishes, and the service would never
finish stopping. The reference is also what lets the service wait for the stop.

When embedding services with `await service.stop()`, the caller owns the event
loop and any unfinished tasks. They can continue running on that loop. In
particular, wrapping the application in ordinary `asyncio.run()` still performs
its own unlimited task join at exit. Use the synchronous service entry points
when Cliffracer should own this policy, and make handlers propagate cancellation
whenever possible.

### Stream fields

A `StreamSpec` names a stream and what the service asks the server for. A field left out
takes its default.

- `name`: the stream's name on the broker. Required. It holds no wildcard, dot, slash, backslash or white space.
- `subjects`: the subjects the stream claims, at least one. They are literal: the namespace is
  never put in front of them for you. A subject has no empty token, white space or control
  character, and `>` only as its last token; two subjects of one stream do not overlap or repeat.
- `storage`: `"file"` (the default) or `"memory"`.
- `retention`: `"limits"` (the default), `"interest"` or `"workqueue"`. A `"workqueue"` stream delivers each message to one consumer, so two of the service's durable listeners whose subjects overlap on it are refused when the service starts, with a `StreamDeclarationError` naming the stream, both subjects and both handlers. Consumers other services create on the same stream are not visible to that check, and the server still refuses an overlapping one.
- `max_age_seconds`: how long the stream keeps a message, a finite number of seconds of 0 or more. `None` (the default) sets no age limit.
- `duplicate_window_seconds`: the window, `120.0` seconds by default, in which the server drops a
  message that repeats a message id it has already stored. `0` does not turn deduplication off: the
  server stores a zero window as its default and reports `120.0` back, so a stream declared with `0`
  has the two-minute window. The server refuses a window longer than the stream's age, so for a
  `max_age_seconds` under 120 the default window, left out or `0`, is that age. A window set longer
  than `max_age_seconds` raises a validation error naming both fields when the `StreamSpec` is built. The window is a finite number of seconds, 0 or more. A dump of a `StreamSpec` that left the window out leaves it out, so validating the dump gives the same declaration.
- `allow_msg_schedules`: whether a message published to the stream may carry a schedule for the
  broker to fire later (nats-server 2.12 and later; see Publishing an event at a later time).
  `False` by default, and then the field is not sent, so an older broker is never asked for it. A
  stream that allows schedules also declares a subject with a `_sched` token, where the schedules
  are published, or it cannot be built. A broker that does not report the field back (one before
  2.12 ignores it) is a difference from the declaration when the service starts and the stream
  already exists. The start that creates the stream refuses nothing, so on a broker before 2.12
  the floor is reported by `publish_at`, and by every start after the first.
- `max_msgs`: the most messages the stream holds, a whole number, 1 or more. `None` (the default)
  declares none: the stream is created without that limit, and an existing stream keeps whatever
  limit it has. `0` and `-1` cannot be built, since the server reads both as no limit; to declare
  none, leave the field out.
- `max_bytes`: the most bytes the stream holds, read as `max_msgs` is.
- `discard`: what the server does at a limit, `"old"` (drop the oldest message) or `"new"` (refuse
  the new one). `None` (the default) declares neither: `"old"` on creation, the stream's own
  otherwise. `"new"` with neither `max_msgs` nor `max_bytes` cannot be built, since it would change
  nothing.
- `num_replicas`: the fewest copies of the stream a cluster keeps, 1 to 5. More copies than declared
  is not a difference. `None` (the default) declares none: one on creation, the stream's own
  otherwise. A stream's configured count is not taken as its copies: a single server refuses more
  than one when a stream is created but accepts a raised count on an update and reports it while
  keeping one copy, so the copies are counted from the stream's cluster information, the leader and
  every peer it lists, current or catching up.

A declaration that holds a value from the list above cannot be built: `StreamSpec(...)` raises a
validation error naming the stream and the value. `ensure_streams` checks every declaration again
before it creates the first stream, and raises one `StreamDeclarationError` naming each stream it
refuses, so a bad declaration never leaves the ones before it on the broker.

A file stream with the default retention, no `max_age_seconds` and no `max_msgs` or `max_bytes`
keeps every message until the server's own limits stop it, so declare an age or a limit on a stream
whose messages are not worth keeping for ever.

### Changing a declared stream

What a broker reports for a field left out was measured on nats-server 2.10.29, 2.11.2 and 2.12.0:
a limit comes back as `-1` (no limit), a discard policy as `"old"`, a replica count as `1`; a
declaration is compared with those, so leaving a field out never reads as a difference.

| change to an existing stream | `'provision'` with `jetstream_update_streams=True` | `'bind'` |
|---|---|---|
| a declared field the stream does not hold | applied | refused, naming the field, the declared and the held value |
| `max_msgs` or `max_bytes` raised, removed from the broker's view, or lowered to no less than the stream holds | applied | refused as a difference |
| `max_msgs` or `max_bytes` lowered below what the stream holds | refused as an operator action, naming the field, the declared limit and what the stream holds: the server would discard the messages over it | refused as a difference |
| `discard` changed | applied | refused as a difference |
| `num_replicas` raised | applied, then refused as an operator action if the copies counted afterwards are fewer than declared ("configured 3, 1 live, not clustered" on a single server); the refusal comes after the update, so the raised count stays on the broker | refused while the configured or the counted copies are fewer than declared |
| a field the declaration leaves out | kept as it is on the broker | not compared |

Without `jetstream_update_streams`, provision mode refuses any difference and changes nothing.
Either way, provision mode counts the copies of every stream that declares `num_replicas` once its
streams are in place, and refuses a stream with fewer than declared as an operator action, whether
or not anything was changed. What a stream holds is read when the streams are listed, before any
update, so a message published in between can still be over a lowered limit and be dropped by it.

### Stream subjects are exclusive

A subject may be claimed by exactly one stream. Two streams whose subjects
overlap cannot both exist on a broker, whatever they are named.

```python
jetstream_streams=[
    StreamSpec(name="ORDERS", subjects=["orders.*"]),
    StreamSpec(name="ORDERS_AUDIT", subjects=["orders.*"]),   # refused
]
```

Cliffracer refuses this before the broker sees it, naming both streams and both
subjects:

```
StreamDeclarationError: stream 'ORDERS_AUDIT' claims 'orders.*', which overlaps
'orders.*' already claimed by stream 'ORDERS'. A subject may be claimed by
exactly one stream — narrow one of the two claims.
```

The server refuses it too, as `err_code 10065`, `subjects overlap with an
existing stream` — without saying which stream or which subject.

The claim that most often overlaps is a catch-all dead-letter stream. A service
dead-letters to `dlq.<service>` by default, so `dlq.*` covers every service on
the broker, and two services declaring it are declaring the same claim twice.
Name the subject the service actually uses:

```python
StreamSpec(name="ORDERS_DLQ", subjects=["dlq.*"])             # every service's
StreamSpec(name="ORDERS_DLQ", subjects=["dlq.order_service"]) # this one's
```

`subject_prefix` separates whole deployments rather than streams within one: two
environments may each hold a stream claiming `dlq.order_service`, because the
subjects are `<prefix>.dlq.order_service` and differ.

### A stream subject must not begin with a wildcard

On nats-server 2.10.29 and later a stream subject may not begin with a wildcard. A leading `*`
can match `$JS`, so the server counts it as overlapping the JetStream API subject space and
refuses the stream with `err_code 10052`. Its text mentions no-ack and the JetStream API, and
mentions neither wildcards nor a version. An older broker accepts such a subject.

This matters most for a `cross_namespace=True` listener, which subscribes to `*.<pattern>`. Do
not claim that shape in a stream: list the namespaces instead.

```python
# listener: @listener("events.thing", cross_namespace=True)
StreamSpec(name="THING", subjects=["jorbo.events.thing", "utils.events.thing"])
```

The listener does not change. A consumer may begin with a wildcard even though a stream may not,
which is what keeps a cross-namespace subscription working against a stream that lists its
namespaces.


## Extensions

Optional functionality is declared as class attributes. Each extension ships as
its own distribution, and declaring one is what loads it. The attribute name is
how you reach the extension.

```python
from cliffracer import CliffracerService
from cliffracer_logging import LoggingExtension

class APIService(CliffracerService):
    logging = LoggingExtension(to_nats=True)
```

| distribution | attribute type | provides |
|---|---|---|
| `cliffracer-auth` | `AuthExtension` | JWT auth, `@requires_auth` / `@requires_roles` / `@requires_permissions` |
| `cliffracer-logging` | `LoggingExtension` | structured logging, correlation logging, log-to-NATS |
| `cliffracer-metrics` | `MetricsExtension` | in-process counters over the dispatch hooks |
| `cliffracer-otel` | `OtelExtension` | OpenTelemetry distributed tracing and W3C context propagation |
| `cliffracer-kv` | `KvExtension` | NATS JetStream Key-Value and Object Store integration with bucket TTL |
| `cliffracer-resilience` | `ResilienceExtension` | circuit breaking, sliding-window rate limiting, and resilient RPC |
| `cliffracer-cron` | *(none)* | `@cron` handlers. `CronTimer` subclasses core's `Timer`, so core's timer discovery starts them and there is no extension to declare |
| `cliffracer-dlq` | *(none)* | `cliffracer-dlq`, a read-only command for the dead letters in their stream. It is a tool for operators and declares nothing on a service |
| `cliffracer-cyanide` | `CyanideExtension` | fault injection for testing: delay, raised faults, simulated timeouts and dropped replies |

Core binds two of its own around the ones you declare: `CorrelationExtension`
first, so every declared hook sees a correlation id, and `ValidationExtension`
last, which validates every RPC and async RPC payload against its handler's
annotations after the declared extensions have admitted the message.
Event payloads are not its concern: a `@validated_listener` or typed `@listener`
is validated by the event dispatcher, and a message that fails is dead-lettered
or dropped according to the listener's `on_invalid`, else the service's
`default_on_invalid`, as described under [Message Validation](#message-validation).

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

`AuthConfig` fields: `secret_key`, `algorithm` (`HS256`, `HS384` or `HS512`; another value is refused
when the config is built), `token_expiry_hours`,
`pbkdf2_iterations`, `leeway_seconds`,
`refresh_max_lifetime_hours`. A field it does not have is refused with a `ValidationError`

`refresh_max_lifetime_hours` defaults to `720` (30 days, thirty times the default `token_expiry_hours`): a token is not refreshed once that many hours have passed since the login that began its chain, whatever its own expiry, so a session longer than a month needs a new login. `None` removes the cap, and then a single login grants access indefinitely.
naming it, so a misspelt option fails where the config is built.

`leeway_seconds` (default `0`) is how many seconds of clock skew a token may be off by. It is applied to `iat`, so a token minted by a host whose clock is ahead is not refused as not yet valid, and to `exp`, so it also extends every token's life by that many seconds: an expired token is accepted for that long, and `revoke_token` keeps a revocation for that long too. Set it where several hosts mint or verify tokens with the same key; a service that mints and verifies its own tokens on one clock has no use for it.

`token_expiry_hours` (default `24`) is also the longest lifetime a token may have to be accepted: a token whose `exp` is more than `token_expiry_hours` plus `leeway_seconds` after its `iat` is refused, and so is a token with no `iat`. Every host that mints tokens with the same key must use the same `token_expiry_hours` and `refresh_max_lifetime_hours`, or smaller ones. Another host does not see this one's revocations and may go on refreshing a revoked chain, so `revoke_token` holds a chain's revocation until that longest lifetime (plus the leeway on each side) past the later of now and the chain's refresh cap (`oiat` plus `refresh_max_lifetime_hours`). With `refresh_max_lifetime_hours=None` a revoked chain is kept for the life of the process, and memory grows with the number of revocations.

`secret_key` is a `SecretStr`: `repr`, `str`, `model_dump()` and `model_dump_json()` of the config show a mask rather than the key, and `config.secret_key.get_secret_value()` reads it. A plain `str` is accepted at construction and by assignment, so a key can be rotated with `auth.config.secret_key = new_key`.

`pbkdf2_iterations` is between `MIN_PBKDF2_ITERATIONS` (1,000) and `MAX_PBKDF2_ITERATIONS` (10,000,000), the fewest and the most `verify_password` will accept in a stored record; a value outside that range is refused when the config is built, and a stored record outside it is refused at login (below the floor, with a warning that names the record's count and the floor). A record made at fewer iterations than the service is configured for, but not below the floor, still verifies, and is written again at the configured count the next time its user logs in. A login for a name that is not registered runs the same PBKDF2 as one for a registered name, so response time does not tell the two apart. User ids (`user_1`, `user_2`, ...) are not reused while the process lives.

**Methods:**
- `create_user(username, email, password) -> AuthUser` -- the password is 8 to 128 characters and is not whitespace alone (spaces, tabs, newlines and the Unicode spaces); it is stored as given, whitespace included, and no composition rule applies. A password that breaks either rule raises `ValidationError`
- `authenticate(username, password) -> str` (JWT token)
- `validate_token(token) -> AuthContext | None` -- `None` for anything that is not a valid token of this service: an expired, revoked or undecodable one, one missing a required claim (`exp`, `jti`, `user_id`, `username`, `email`), carrying one of the wrong type, or carrying a `user_id`, `username` or `email` that is empty or whitespace alone, and one for a user this service holds as inactive. A token for a user the service has no record of is accepted, since the store is in memory and a token may come from another issuer sharing the key
- `refresh_token(token) -> str` -- a new token in the same chain as the one given: the `cid` claim, which a login sets to its own `jti`, is carried on Refresh is a re-issue: the token it was given is not consumed and stays valid to its own expiry, and can be refreshed again, so the only bound on how long refreshing keeps a leaked token alive is `refresh_max_lifetime_hours` (below). Revoking either token revokes the chain.
- `revoke_token(token) -> bool` -- revokes the token's whole chain: the tokens refreshed from it, the ones it was refreshed from and their siblings, so a refresh does not outlive the revocation of the token it came from. A separate login is a separate chain. `True` when the token cannot validate afterwards: its `jti` is revoked, or it has already expired and is refused on its own. `False` when the token does not decode or has no `jti` or `exp`. A `jti` is dropped when its token expires, and a chain after one token lifetime
- `add_role(username, role)` -- raises `ValueError` if there is no such user
- `add_permission(username, permission)` -- raises `ValueError` if there is no such user
- `hash_password(password)` / `verify_password(password, encoded)`

Usernames are case-insensitive. `create_user` stores the lowercased name, and
every method that takes a username looks the user up the same way, so "Alice",
"alice" and "ALICE" are one account.

The user store is a dict in the service process, so it lives as long as the
process. It suits tests and small deployments.

### AuthUser

A dataclass.

```python
from dataclasses import dataclass, field
from datetime import UTC, datetime


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

The `AuthUser` a handler gets from a token is a projection of the token's claims: `user_id`, `username`, `email`, `roles` and `permissions` come from the token, `created_at` is the time the token was validated, and `is_active` is always `True` (a token for a deactivated user is refused). The stored record keeps the real `created_at`.

## Decorators

### RPC Decorators

`@rpc` exposes a method as `{service_name}.rpc.{method_name}`, with the namespace in
front when `ServiceConfig.namespace` is set: `{namespace}.{service_name}.rpc.{method_name}`.
Every RPC handler is also reachable, fire-and-forget, on `{service_name}.async.{method_name}`
(namespaced the same way), which is what `call_async` sends to. `@async_rpc` is the same
decorator as `@rpc` in what the framework does with it: the marker it adds records, for the
reader, that the method is meant to be called with `call_async`.

An RPC handler's parameters are the remote arguments, so a name a caller could not pass is
refused when the service starts, and by `describe`: a name starting with an underscore, a
name that is a member of `pydantic.BaseModel`, and `namespace`, which `call_rpc` and
`call_async` take as the routing namespace.

An RPC handler returns one reply unless it streams it (below), so a generator, and an async
generator whose return is not annotated as a stream, is refused the same way, by name: calling it
only builds the generator, and its body would never run.

The description is published as JSON, which has no `inf`, `-inf` or `nan`, so a default or a
bound holding one is refused the same way, whether it sits on a parameter or in a model that an
`@rpc` handler or a `@validated_listener` takes or returns. `float | None = None`, with `None`
meaning no limit, says the same thing.

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

### A handler that streams its reply

An `@rpc` handler written as an async generator, its return annotated `AsyncIterator[X]` or
`AsyncGenerator[X, None]`, streams its reply: it sends each item as it yields it, and `describe`
publishes its return as `{"kind": "stream", "item": <the item's TypeRef>}`.

```python
from collections.abc import AsyncIterator

from cliffracer import CliffracerService, rpc


class Logs(CliffracerService):
    @rpc
    async def tail(self, n: int) -> AsyncIterator[int]:
        for line in range(n):
            yield line
```

On the wire, a request for a stream carries the header `Cliffracer-Stream: 1` and a reply subject
that its caller subscribes before it sends. The service publishes to that subject:

- one message per item, the item's dump validated against `X` as a return is, with the headers
  `Content-Type`, `X-Correlation-ID` and `Cliffracer-Stream-Seq`, the item's index from 0;
- then one envelope, the same as a single reply's, with `"items"` and the header
  `Cliffracer-Stream-End` both giving the number of items sent. Success is
  `{"success": true, "result": null, "items": N}`. A caller that counted fewer items than `N` has
  missed one.

The stream runs inside its request's call, so the whole of it holds one admission slot and one
`max_rpc_concurrency` permit (and the handler's own, under `max_concurrency=`), and is bounded by
one deadline, its caller's budget or `max_rpc_processing_time`. A stream holds its method's
admission slot, counted against `max_concurrency` plus `max_queued`, until it ends: with
`@rpc(max_concurrency=1, max_queued=0)`, another call of the method is answered `busy` while a
stream runs. It ends early with the envelope that says why, after the items already sent:

- an item that does not match `X`, or a raise in the handler, ends with code `internal`; a
  `RejectMessage` ends with `refused`;
- the deadline ends it with `deadline_exceeded`;
- `max_stream_items` or `max_stream_bytes` (the item bodies) ends it with `refused` and
  `"limit": {"items": n}` or `{"bytes": n}`; the item that would pass the limit is not sent.

The handler's generator is closed whatever ends the stream, so its `finally` blocks run.

A request whose `Cliffracer-Stream` header does not match the handler (a stream asked for once, or
a single reply asked for as a stream) is not run and is answered `validation_failed`, with one
detail of type `stream_mismatch`. A streaming handler's fire-and-forget subject runs nothing and
logs a warning, since nobody would hear its items; `@async_rpc` on one is refused when the service
starts.

Each item is published with a reply subject under the service's `nats_inbox_prefix`, which one
plain subscription on the service's connection holds. When the caller has gone (it unsubscribed,
or its connection closed), the broker answers each item after that there with a no-responders
status. The status arrives a little after the item it answers, so the stream stops a few items
later, without an envelope, its generator closed. The broker sends
that status only to the publishing connection, and only on a subscription outside a queue group.

Under broker permissions, `broker_permissions` grants a service with a streaming method
`allow_responses` with no limit on replies, and needs `max_rpc_processing_time` set and
`response_ttl` at least that long plus a one-second margin (see
[NATS broker permissions](broker-permissions.md)). A grant of one reply per request cuts a stream
after its first message.

#### Reading a stream

A generated client's method for a streaming handler, `CliffracerService.stream_rpc` and an
`RpcProxy` method's `.stream(...)` all read a stream the same way:

```python
async for line in client.tail(n=100):  # a generated client
    ...
async for line in self.stream_rpc("logs", "tail", n=100):  # untyped, from a service
    ...
async for line in self.logs.tail.stream(n=100):  # through an RpcProxy
    ...
```

- Each item is yielded as it arrives. A generated client validates it against the item type the
  service describes, and an item that does not match raises `RpcServerError`; `stream_rpc` and
  `.stream(...)` yield the decoded JSON.
- An error the stream ends with is raised after the items before it, as the same class a single
  reply's error is, and the exception's `items` says how many arrived. An item that never
  arrived, out of order or missing at the end, raises `RpcStreamGapError` with `expected`, `got`
  and `items`.
- One timeout bounds the whole stream: the client's `timeout`, or `request_timeout` for
  `stream_rpc`, either cut to what an enclosing request has left, and sent as the budget in
  `Cliffracer-Timeout-Ms`. When it runs out the stream raises `RpcTimeoutError`, or its subclass
  `RpcDeadlineExceededError` when the service's end of the same budget came first.
- The reply inbox is unsubscribed when the stream ends, when it fails (a gap, an item that does
  not match, a timeout), and when the generator is closed, and the service stops soon after. Left
  early with `break`, an abandoned generator is closed when CPython finalises it, a few loop turns
  later; under `contextlib.aclosing` it is closed, and the inbox unsubscribed, as the block exits:

  ```python
  async with contextlib.aclosing(client.tail(n=100)) as lines:
      async for line in lines:
          if line.number == 3:
              break
  ```
- The timeout also bounds each wait for the next message, so a stream that falls silent raises
  `RpcTimeoutError` at its timeout, with `items` saying how many arrived.
- A plain `await self.logs.tail(n=100)` on a streaming method raises `RpcValidationError`
  naming `.tail.stream(...)`.
- `stream_rpc` runs the send hooks once, around opening the stream (subscribing its inbox and
  sending the request), with `ctx.kind` `stream_rpc`: a hook that counts calls counts a stream
  once, whatever the number of its items.

### Calling a service without a generated client

`cliffracer.calls` calls a running service over a connection you hold, with no generated client:
`call` for one reply, `stream` for a streamed one.

```python
import nats

from cliffracer.calls import call, stream


async def report(url: str) -> None:
    nc = await nats.connect(url)
    total = await call(nc, "orders", "total", {"customer": "c-1"}, timeout=5.0)
    async for line in stream(nc, "logs", "tail", {"n": 100}, idle_timeout=2.0):
        print(total, line)
    await nc.close()
```

- `params` is a mapping of JSON-ready values: the method's arguments by name. `namespace` and
  `subject_prefix` address the subject as the service subscribes it (`subject_prefix` defaults to
  `CLIFFRACER_SUBJECT_PREFIX`; `""` is unprefixed), and `headers` adds headers of your own.
- The request is labelled `Content-Type: application/json`, carries the correlation id (one in
  `headers`, the ambient one, or a new one), and carries `timeout` as its `Cliffracer-Timeout-Ms`
  budget, cut to what an enclosing request has left (a budget given in `headers` is sent as given);
  a call with none left is not sent.
- An error the service answers is raised as `call_rpc` raises it; a call nothing holds raises
  `RpcNoRespondersError`, and one not answered within `timeout` raises `RpcTimeoutError`.
- `stream` reads as a generated client does (each item as it arrives, an error after the items
  before it with `items`, `RpcStreamGapError` for a lost item, the inbox unsubscribed however it
  ends), with one more bound: `idle_timeout`, when given, ends a stream that sends nothing for that
  long with `RpcTimeoutError` naming it. `timeout` bounds the whole stream.
- `prepare(service, method, params, ...)` builds and checks the request `call` and `stream` send,
  without sending it, and returns its `subject`, `headers`, `payload` and `timeout` (the wait):
  what a dry run shows. `cliffracer call --dry-run` prints it.
- `timeout=None` sets no bound of the caller's own: no wait limit and no budget header (inside a
  handler the request's remainder is still the wait and the budget). It is the one way to say
  unbounded: `inf`, NaN, zero and negatives are refused.
- `timeout` and `idle_timeout` must otherwise be positive, finite numbers of seconds, and are
  refused by name; a service or method name that makes a subject the server cannot use (whitespace,
  an empty or doubled `.`) is refused with `ValueError`, as `call_rpc` refuses it. Every argument
  is checked when `call` or `stream` is called, before anything is sent.

What an untyped call gives up, against a generated client's method:

- **No encoding through declared types.** The arguments go out as `json.dumps` writes them. A value
  it cannot write is refused before sending (`RpcClientError`), but a value the service would read
  as something else (a string for a date, a float for an int) is not caught here. A pydantic model
  anywhere in `params` is refused by name: which form of it the service reads is the generated
  client's to choose, so call the method through one, or pass the model's dump.
- **No signature check.** The service's signature hashes are not compared, so a call to a method
  whose parameters changed is answered as the service now reads it, never refused as out of date.
- **No result type.** The result is the reply's decoded JSON, and each streamed item the item's,
  not validated against a declared type.

### Event Decorators

```python
@listener("user.created", fanout=True)
async def on_user_created(self, subject: str, user_id: str) -> None:
    """Subscribe to specific event"""
    pass

@listener("order.*", fanout=True)
async def on_any_order_event(self, subject: str, order_id: str) -> None:
    """Subscribe to pattern"""
    pass
```

A listener's default is validated and coerced as a sent value is: when an event omits
`count: int = "5"`, the handler receives `5`.

A listener whose only parameter is a model, `on(self, item: Item)`, reads the event's payload as
that model, so `publish_event(topic, name="a")` delivers `Item(name="a")`. It reads the form
`publish_event(topic, item=Item(name="a"))` sends, `{"item": {"name": "a"}}`, as the same model,
when no field of the model is named or aliased `item` (by its alias or a validation alias: each
`AliasChoices` member, and the first element of an `AliasPath`) and the payload holds that one key
(and the `correlation_id` every event carries) with an object under it. Any other payload is read
as the model itself: a model with a field named or aliased `item`, a `RootModel`, a payload with
another key besides `item`, and a value under `item` that is not an object. A model that declares
a `correlation_id` field keeps it as one of its keys, so a payload carrying it besides `item` is
read as the model itself. A model whose fields all have defaults and that keeps extra keys
(`extra="allow"`) reads a lone `{"item": {...}}` as the nested form too, not as itself with `item`
as an extra key: the two readings are the same payload, and the nested one is taken. A
`@validated_listener` reads its schema by the same rule, under the name of its handler's parameter:
`publish_event(topic, message=Schema(...))` is read as the `Schema` for `on(self, message: Schema)`,
and an all-defaults `extra="allow"` schema sent a lone `{"message": {...}}` is read as the nested
form.

### Pull listeners

`@listener("orders.created", durable="orders", pull=True)` binds the durable as a pull
consumer instead of a push one. The replica fetches when it has capacity, so the work it
holds at once is bounded by `jetstream_max_ack_pending`; a fetch asks for
`jetstream_pull_batch` messages and waits `jetstream_pull_timeout` seconds. Startup refuses
`pull=True` without a durable, with `fanout=True` (a pull consumer delivers each message to one
replica) and without `jetstream_enabled`. `@validated_listener` has no `pull` parameter.

### Pausing a listener while a dependency is down

```python
from cliffracer import CliffracerService, ServiceConfig, StreamSpec, dependency, listener


class Fulfilment(CliffracerService):
    def __init__(self) -> None:
        super().__init__(
            ServiceConfig(
                name="fulfilment",
                jetstream_enabled=True,
                jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.>"])],
            )
        )

    @dependency("postgres", timeout=1.0)
    async def _check_db(self) -> None:
        await self.pool.execute("SELECT 1")

    @listener("orders.created", durable="fulfilment", pause_when_down=("postgres",))
    async def on_order(self, subject: str, order_id: str) -> None:
        ...
```

`pause_when_down` (on `@listener` and `@validated_listener`) names declared dependencies, from
`@dependency` or from `add_dependency` called before the service starts. While any of them is
down, this replica stops consuming the listener, and it starts again when they are all up. The
messages wait in the stream instead of being delivered, failed, NAKed into backoff and, at the
delivery limit, dead-lettered.

- **How it is decided.** The dependencies some listener names, and only those, are probed in the
  background every `dependency_probe_interval` seconds (5 by default), with the same bounds as the
  probes `/ready` runs. A dependency counts as down after `dependency_pause_after` failed probes
  in a row (2) and as up after `dependency_resume_after` passes in a row (2), so a dependency
  that fails and passes by turns does not pause anything. `/ready` and `/health` keep running
  every probe per request, as they do without this.
- **How it pauses.** The replica drops its interest in the listener's durable: a push
  subscription is unsubscribed, a pull loop stops fetching. The durable and its messages stay on
  the server. Nothing is delivered to a durable no replica is bound to, so a paused listener
  spends no delivery attempt. A replica that pauses while others do not leaves the work to them,
  and its unacknowledged messages go to them once their `ack_wait` passes. Messages already being
  handled when the pause comes finish as they would have. Resuming binds the durable again.
- **What it says.** One warning on each pause and one line on each resume, naming the listener
  and the dependencies. While a listener stays paused, a warning every 10 probe intervals names
  it, the dependencies, how long it has been paused and whether its stream has a
  `max_age_seconds`, past which the stream removes messages that are still waiting. `/health`
  lists `paused_listeners` (subject, `since`, `seconds`, `dependencies`), and extensions hear
  `on_listener_paused` and `on_listener_resumed`, from which `MetricsExtension` keeps a
  `listener_paused` gauge per subject.
- **What it does not do.** Nothing resumes a listener on a timer: one whose dependency never comes
  back stays paused. It does not pause RPC handlers. It works on any broker the service runs on,
  and does not use the server's consumer pause API.

Startup refuses `pause_when_down` on a listener with no durable or with `fanout=True` (a core
subscription would lose what is published while it is paused), on a service with
`jetstream_enabled=False`, and naming a dependency the service does not declare.

### A concurrency limit per handler

`max_concurrency=n` on a handler's decorator runs at most `n` calls of that method at once:
`@rpc(max_concurrency=4)`, `@async_rpc(max_concurrency=4)`, `@listener(..., max_concurrency=1)`,
`@validated_listener(..., max_concurrency=1)` and `@broadcast(pattern, max_concurrency=1)`. `@rpc`
and `@async_rpc` used bare set no limit. The limit is a positive `int`, refused otherwise when the
handler is declared, and so is `@rpc(4)`, a limit where the handler goes.

```python
@rpc(max_concurrency=2)
async def monthly_report(self, month: str) -> dict[str, str]:
    """At most two run at once; other methods are not held back by the ones waiting."""
    ...
```

A method has one limit across everything that reaches it: its `rpc` and `async` subjects, every
pattern it listens on, and an `@rpc` and a `@listener` on the same method. Two decorators that
give one method different limits are refused.

A request over the limit waits for the method's permit, then for the service's
(`max_rpc_concurrency`, `max_async_rpc_concurrency` or `max_event_concurrency`). Waiting for the
method's permit, it holds no service permit, so requests queued at one full method do not hold
back any other:

- An RPC request waits on its own task, as described under Admission and the wait for a permit.
  Its deadline bounds both waits; one that passes first is answered `deadline_exceeded` and not
  run, and a method permit already taken is returned. `max_rpc_in_flight` admits requests before
  either wait.
- A core event waits in its subscription's callback. That subscription carries only that
  pattern's messages, so it holds back only messages for the same handler.
- A JetStream message waits on its own task, sent in-progress pulses while it waits, on push and
  pull listeners alike.

A request waiting at a full method still holds its `max_rpc_in_flight` slot: it has been admitted.
`max_queued=q` on `@rpc` and `@async_rpc`, with `max_concurrency`, is the most requests that may
wait at the method when it is full. A request that finds `max_concurrency + max_queued` of the
method's requests already admitted is answered `busy` at once, naming the method, with `limit` and
`in_flight` the method's (a fire-and-forget request is dropped and logged), and it takes no
admission slot. So one slow method cannot fill the admission count and leave every other method
answering `busy`.

```python
@rpc(max_concurrency=1, max_queued=20)
async def monthly_report(self, month: str) -> dict[str, str]: ...
```

- **Unset, with `max_rpc_in_flight` set,** a limited method's `max_queued` is half that bound,
  rounded down and at least 1, so the other methods always keep slots.
- **With no `max_rpc_in_flight`** nothing is admitted against a count, so there is no default cap.
- **`max_queued=0`** turns a request away whenever the method is full.
- **The value** is an `int` of 0 or more, refused otherwise when the handler is declared, and so
  is `max_queued` without `max_concurrency`. As with the limit, one method has one value.
- **The listener decorators refuse `max_queued`.** An event takes no admission slot: a core
  listener's wait holds only its own pattern's messages, and a JetStream message waits on the
  broker.

`/health` has `handler_limits` when any handler declares a limit, keyed by method name:

| field | meaning |
|---|---|
| `limit` | the declared limit |
| `max_queued` | the most requests that may wait at the full method, declared or the default; `null` for no bound |
| `in_flight` | calls holding the method's permit |
| `waiting` | requests waiting for the method's permit, or holding it and waiting for the service's |
| `refused` | requests not started: turned away over `max_queued`, or waited at the limit and their deadline passed or the service was stopping (cumulative) |

`describe()` does not include the limit: a caller reaches a limited method on the same subjects
and gets the same replies.

### Timer Decorators

```python
@timer(interval=60)  # Every 60 seconds
async def periodic_task(self):
    """Run periodically"""
    pass
```

`@timer` takes `interval` and `eager`, plus `headers` (passed in the `WorkerContext` of each
firing) and `token_factory`, a callable returning a bearer token. A firing sends the token as
its `authorization` header, and a service whose `AuthExtension` has `allow_timers=False`
refuses a timer that carries none. Other keywords, such as `max_drift` and `error_backoff`,
are passed to `Timer`; an unknown one is a `TypeError`.

`deadline=` bounds each firing, `@cron`'s as well as `@timer`'s, the eager one included:

```python
@timer(interval=60, deadline=20)  # each firing has 20 seconds, its hooks included
async def sync_inventory(self):
    await self.call_rpc("warehouse", "stock_levels")  # waits at most what is left of the 20
```

- **While the firing runs, the deadline is the current one.** A call it makes through `call_rpc`,
  `RpcProxy` or `ServiceClient` waits at most what is left, and sends that as its budget, as a call
  from an RPC handler does.
- **A firing still running at its deadline is cancelled and counted as an error:** `error_count`
  goes up, `last_error_type` is `"DeadlineExceeded"`, and `last_error` says how long it was given
  and had run. One that suppresses the cancellation and returns is still counted as cut off. The
  schedule carries on.
- **Unset, a firing has no deadline** and runs until it returns. Firings never overlap: the next
  one is scheduled after the last returns, so a long firing delays the next rather than running
  alongside it.
- **The value** is a finite number of seconds above zero, refused otherwise where the timer is
  declared.
- **It is on the event loop's clock,** as a request's deadline is, not on the timer's `clock`: a
  `FakeClock` moves the schedule, not the deadline.
- **A distributed `@cron` with `no_overlap`** refuses a `deadline` longer than its `lease_ttl`. A
  firing still running when its lease ends would be overlapped by another replica's.

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
    RpcConnectionError,       # a client call that cannot reach the broker
    ConfigurationError,       # Config problems
    ValidationError,          # A framework argument check failed (a timeout, a batch size, a string)
    AuthenticationError,      # Auth failures
    AuthorizationError,       # Permission denied
    RPCError,                 # RPC call failures
)
```

A call's own errors import from `cliffracer`: `RpcTimeoutError` when no reply came in time, and
its subclass `RpcDeadlineExceededError` when the service cut the handler off at the request's
deadline and said so; `RpcBusyError`, a `RpcServerError`, when the service did not take the
request; `RpcStreamGapError`, a `RpcError`, when a streamed reply lost an item.

`ValidationError` is what the framework's argument checks (`validate_timeout` and its
siblings) raise; it is also a `ValueError`. A pydantic model, a typed handler's arguments and
`ServiceConfig` raise pydantic's own `ValidationError`, which is a different class: import it
from `pydantic` to catch those.

Handle these with ordinary `try`/`except` in your handlers. An exception that
reaches the container is returned to the caller as an error reply and counted
by `MetricsExtension` if it is installed.

JetStream startup can also raise `StreamDeclarationError` for a missing or
incompatible declared stream and `ConsumerBindingError` when bind mode cannot
adopt a pre-provisioned durable consumer. Both import from `cliffracer` and are
`ConfigurationError` subclasses.

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
keyword arguments become permanent context on every line it writes, and every
line carries `service` set to `service_name`. A `service` in the context, or
passed on one call, replaces it for those lines.

### LoggingConfig

`LoggingConfig` is configured through one static method, not constructed:

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
    replace_existing=True, # remove every sink loguru holds first; False adds alongside
)
```

`configure` returns the ids of the sinks it added, so a caller can remove just
those with `logger.remove(id)`. It merges `service` into the process's global
loguru `extra`, so context the host application set stays on every record;
records written through the plain `logger` carry the service configured last,
and a `ContextualLogger` labels its own lines.

## Utilities

### Testing a service in process

`ServiceTestHarness` from `cliffracer.testing` runs a service's handlers in process, over
mock message envelopes, with no broker and no socket. Build it from the class, with a
`ServiceConfig` if the defaults (`name="test_harness_svc"`, no health port) are not what the
test needs, or from an instance, whose config is its own; passing `config=` with an instance
raises `TypeError`.

```python
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing import ServiceTestHarness


class Orders(CliffracerService):
    @rpc
    async def place(self, sku: str) -> int:
        return 1


async with ServiceTestHarness(Orders, config=ServiceConfig(name="orders", health_port=0)) as harness:
    reply = await harness.rpc("place", sku="bolts")
    assert reply.success and reply.result == 1
```

Entering the block (or calling `setup()`) runs extension `setup()`, discovers handlers and runs
extension `start()`, in the order a live start does. Leaving it (or `teardown()`) drains the
tasks still running within `drain_timeout`, cancelling what is left, and stops the extensions; a
harness that has been torn down raises `RuntimeError` rather than being reused.

- `rpc(method, *, payload=None, headers=None, format="json", **kwargs)` calls a handler and
  returns a `TestResponse`: `data` is the decoded reply, `result` the handler's return value,
  `error` the error text, and `success` is `False` for an error envelope and also for a handler
  that sent no reply.
- `emit_event(subject, data=None, *, pattern=None, raise_on_error=True, ...)` (also `publish`)
  dispatches an event as the service would receive it, and returns the `DispatchOutcome`. The
  subject is namespaced the way a `@listener` subject is, so it is written as the decorator wrote
  it. A subject no handler matches returns `NO_HANDLER`. An exception in a handler propagates
  unless `raise_on_error=False`, and so does a failing `fails_closed` extension; a policy refusal
  an extension raises is not an error, and the handler is skipped.
- `describe()` returns the description dict, and raises `RuntimeError` when the service sent no
  reply or answered with a failure envelope.
- With `jetstream_enabled=True` the harness installs an in-memory JetStream context, exposed as
  `jetstream`, and `deliver_jetstream(subject, data, *, num_delivered=1, ...)` returns the
  `MockMessage` so a test can read whether it was acknowledged, redelivered or terminated.
  With JetStream off both raise `RuntimeError`, since the service would never take that path.

What the harness does not run: timers are not started unless the test starts them with
`start_timers()` (below), the health listener is not started, no durable consumer is read from a
server, and nothing leaves the process. `MockMessage(subject, data=b"",
headers=None, reply="_INBOX.test")` is the envelope it builds, and is usable on its own; like a real
message, it refuses `respond` when it has no reply subject.

`ServiceTestHarness(service, broker=...)` runs the service differently: entering the block runs
the service's own `start()` over a connection from `broker`, any object whose
`async connect(url, **options)` returns a connection, and leaving it runs `stop()`. That is the
whole start sequence, timers, subscriptions and the health listener included: with
`health_port=0` the service binds one ephemeral loopback port, and teardown releases it. Several
harnesses given one broker are several services reaching each other by subject. `jetstream`
raises `RuntimeError` on such a harness: what the service published is on the broker.

### Several services in one process: `InMemoryBroker` (provisional)

`InMemoryBroker` from `cliffracer.testing` is a broker in the test process. Give it to each
service's harness, and take a connection from it for a client, and they reach each other by
subject with no server and no socket of their own:

```python
import contextlib

from cliffracer import CliffracerService, rpc
from cliffracer.testing import InMemoryBroker, ServiceTestHarness


class Billing(CliffracerService):
    @rpc
    async def charge(self, order_id: str) -> str:
        return order_id


broker = InMemoryBroker()
async with contextlib.AsyncExitStack() as stack:
    for service in (Orders, Billing):
        await stack.enter_async_context(ServiceTestHarness(service, broker=broker))
    client = await broker.connect()
    reply = await client.request("orders.rpc.place", b'{"order_id": "o-1"}', timeout=5.0)
    await broker.settle()
    await client.close()
```

A generated client takes a connection the same way, through its `nc=` argument.

What it models, each behaviour also checked against a real broker in CI, by cases that run
against both:

- publish and subscribe, with headers carried as sent, and `headers` is `None` when none were;
- subject matching by token: `*` is one token, a trailing `>` one or more, every other token is
  matched as written;
- queue groups: each message goes to one member of each group, the members taking turns, and to
  every subscriber in no group;
- request and reply, the reply carrying the headers its responder set. A request nothing listens
  on raises `NoRespondersError` at once, and one nothing answers raises
  `nats.errors.TimeoutError`;
- a response grant's reply count. After `broker.allow_responses(user, response_max)`, a
  connection dialled as `user` (a service's `nats_user`) may publish `response_max` messages to
  the reply subject of each request delivered to it, `-1` for no limit, as a broker user's
  `allow_responses.max` allows. A publish past the count is not delivered: the connection's
  `error_cb` gets `nats: permissions violation for publish to "<subject>"`, as from a broker. So a
  service whose role grants one reply has its stream cut after the first item here too;
- `unsubscribe`, and `close` on one connection, which leaves every other connection delivering;
- delivery on a task per subscription, in order per subscription, so a callback runs after
  `publish` returns. A callback that raises does not stop the next delivery; its error goes to
  the connection's `error_cb` and to `broker.handler_errors`.

`await broker.settle(timeout=5.0)` returns once every delivery handed out so far has run, and
raises `AssertionError` naming how many are still in flight if that takes longer; it does not wait
for tasks a callback spawned. `broker.published` (records of `subject`, `data`, `headers`,
`reply`), `broker.subscribed` (`subject`, `queue`) and `broker.handler_errors` are read-only views
of what went over it. `connect()` accepts the URL and options a dial passes and reads only
`user`, for its response grant. A connection a test opens itself is closed by that test; one a harness opened is closed by
its teardown.

What it does not model: a response grant's expiry and every other broker permission; and
JetStream, so neither KV nor the object store. `jetstream()` on a
connection raises `NotImplementedError`, and so does starting a service with
`jetstream_enabled=True` on it. Run those against a broker.

`InMemoryBroker` is provisional: its API may change in the next minor release without a
deprecation; it becomes stable in the release after that unless that release says otherwise.

### Timers on a fake clock

A `Timer` (and a `@cron` timer) reads the time and waits through its clock, the real one unless
it is given another: `Timer(interval=..., clock=...)`. `FakeClock` from `cliffracer.testing` is a
clock whose time moves only when the test moves it, so a schedule is tested in milliseconds and
its count is exact.

`ServiceTestHarness.start_timers(clock=)` starts every timer the service declared on that clock,
after setting the harness up if it is not. The harness stops them when it is torn down. A harness
given `broker=` starts the service's timers itself, under `start()`, and refuses `start_timers`
with `RuntimeError`.

```python
from cliffracer import CliffracerService, ServiceConfig, timer
from cliffracer.testing import FakeClock, ServiceTestHarness


class Poller(CliffracerService):
    polls = 0

    @timer(interval=60)
    async def poll(self) -> None:
        self.polls += 1


clock = FakeClock()
async with ServiceTestHarness(Poller, config=ServiceConfig(name="poller", health_port=0)) as harness:
    await harness.start_timers(clock=clock)
    await clock.advance(180)
    assert harness.service.polls == 3
```

- `advance(seconds)` moves the monotonic and the wall time forward together. Each wait that ends
  inside the step is woken in deadline order, and after each wake `advance` waits, in real time
  bounded by `settle_within` (5 s by default), until every task that waits on the clock is waiting
  on it again or has finished; a task that does neither within that bound makes `advance` raise
  `AssertionError` naming it. A handler that awaits something other than the clock holds `advance`
  until it returns.
- `step_wall(seconds)` moves only the wall time, as a wall clock that is set does. An interval
  timer schedules on the monotonic time and is not moved by it; a cron timer reads the wall time
  for its occurrences.
- `FakeClock(start=...)` takes the wall time it starts at, an aware `datetime`
  (2026-01-01T00:00:00Z by default). Time is kept in whole microseconds.
- A timer started without the harness is passed to `clock.watch(timer.task)` after `start()`, so
  the first `advance` waits for it to reach its first wait.

The clock governs the schedule: the interval and occurrence waits, the error backoff and the
execution timing. A stop's grace and the timestamps a distributed cron lease shares with other
replicas stay on real time. `cliffracer.core.clock.Clock` is the protocol a clock implements:
`monotonic()`, `now(tz)`, `async sleep(seconds)` and `async wait(event, timeout)`, which returns
whether the event was set.

### Condition waits in tests

`wait_until(condition, *, within, reason)` from `cliffracer.testing` polls a
synchronous observation and raises an `AssertionError` that includes `reason`
along with the elapsed wait and configured budget when the condition never
becomes true:

```python
from cliffracer.testing import wait_until

shipment_ready = False  # Set by the worker startup path.
await wait_until(
    lambda: shipment_ready,
    within=5,
    reason="the shipment worker reports ready",
)
```

Use it for positive readiness and completion observations. To prove an event
did not happen, first wait for the producer's completion barrier and then
assert that the event is absent. Waiting for `lambda: not events` succeeds
before the producer has a chance to run and proves nothing about the completed
operation.

### Input Validation

`cliffracer.core.validation` is an internal helper module. It contains
`validate_batch_size`, `validate_password`, `validate_string_length`,
`validate_timeout` and `validate_username` for the library's own use, and it is
reached by that full path.

`validate_timeout(timeout, min_ms=None, max_ms=None)` takes **seconds** and
returns seconds; its bounds are in **milliseconds**, because `NumericBounds`
states them that way and `MIN_TIMEOUT_MS = 1` has no whole-second equivalent.
Every refusal names which unit it means. A timeout that is not a finite number,
or is a `bool`, is refused rather than converted.

Validate your handler arguments by annotating them, with Pydantic models where
a parameter is structured, and your event payloads with `@validated_listener`.
That is the supported path, and the one the framework enforces at the
boundary.

### Environment Helpers

Settings are fields on `ServiceConfig`, set in code or in a
`cliffracer run --config` YAML file. An application reads its own environment
with `os.environ`. Extensions read their own prefixed variables — see the table
in the README.
