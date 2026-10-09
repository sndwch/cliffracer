# cliffracer-resilience

Circuit breaking and rate limiting for Cliffracer microservices.

## Overview

High-throughput microservices need to protect themselves from cascading failures and traffic spikes:
- **Circuit Breaking**: When a downstream dependency fails repeatedly, requests fail fast locally without sending wire traffic, giving the downstream service time to recover.
- **Rate Limiting**: Enforces call rate thresholds per handler or per key using in-memory or distributed NATS KV sliding window algorithms, rejecting excess requests over the wire via clean `RpcRefused` responses.

## Features

### Circuit Breaker
- **State Machine**: `CLOSED` -> `OPEN` -> `HALF-OPEN` -> `CLOSED`.
- **Fast Fail**: When in `OPEN` state, requests raise `RpcCircuitOpenError` locally without placing messages on the NATS wire.
- **Cooldown & Probing**: After `recovery_timeout`, transitions to `HALF_OPEN` to permit a probe request. Success resets to `CLOSED`; failure trips back to `OPEN`. An application exception outside `monitored_exceptions`, or cancellation of a half-open probe, returns its admission slot so a later probe can determine the downstream service's health. Each request retains the state under which it entered, so older in-flight work cannot decide the outcome of a later recovery probe. `call_async` is the exception to the probe budget: it is refused while the circuit is OPEN and admitted without limit while it is HALF_OPEN, taking no probe slot, because a fire-and-forget call has no reply to decide a probe. `half_open_max_calls` bounds the awaited calls only, so a service that fires many `call_async` calls while its dependency is recovering sends all of them.
- **What Counts**: Only the errors of a failing or unreachable dependency count toward opening: `RpcTimeoutError`, `RpcNoRespondersError`, `RpcConnectionError` and `RpcServerError`. A builtin `ConnectionError` or `TimeoutError` that propagates through the breaker is not among them, since the client has already turned a lost or silent connection into an `RpcConnectionError` or `RpcTimeoutError`; name it in `monitored_exceptions` to count it. A reply that blames the caller -- invalid arguments, an unknown method, a refusal by policy such as a rate limit, an out-of-date client -- shows the dependency is answering, so it never opens a circuit. `CircuitBreakerConfig.monitored_exceptions` replaces that set entirely.
- **Integration**: Replaces standard `RpcProxy` with `ResilientRpcProxy`.

### Rate Limiting
- **`@rate_limit(calls=100, window=60)` Decorator**: Declare call limits per time window directly on RPC and event handlers.
- **Dual Backends**:
  - `InMemoryRateLimiter`: Local sliding window using timestamp queues, for one process and one event loop.
  - `KvRateLimiter`: Distributed sliding window using NATS JetStream KV with optimistic concurrency and fail-closed decisions.
- **Capacity Refusal**: Excess RPC calls surface as `RpcRefused`. Durable events are NAKed with the limiter's availability delay, then dead-lettered if they exhaust the consumer's delivery limit.
- **Extension**: `ResilienceExtension` enforces rate limits in the `worker_setup` hook before handlers execute.

## Usage

### 1. Resilient RPC Proxy (Circuit Breaker)

```python
from cliffracer import CliffracerService, rpc
from cliffracer_resilience import ResilientRpcProxy, CircuitBreakerConfig

class OrderService(CliffracerService):
    # Protect outbound calls to payment service
    payment = ResilientRpcProxy(
        "payment_service",
        config=CircuitBreakerConfig(
            failure_threshold=5,
            recovery_timeout=30.0,
            half_open_max_calls=1,
        ),
    )

    @rpc
    async def process_order(self, order_id: str, amount: float) -> dict[str, str]:
        # Fails fast locally with RpcCircuitOpenError if payment service is down
        result = await self.payment.charge(order_id=order_id, amount=amount)
        return {"status": "paid", "result": result}
```

`failure_threshold` and `half_open_max_calls` are at least 1, and `recovery_timeout` is 0 or more
seconds; a `CircuitBreakerConfig` outside that is refused when it is built. `monitored_exceptions`
is a tuple or list of exception classes. A proxy takes `circuit_breaker=` (a breaker, which carries
its own config) or `config=`, not both. `breaker.last_state_change` is when the circuit last
changed state on the `time.monotonic` clock, including when it went half-open.

### 2. Handler Rate Limiting

```python
from cliffracer import CliffracerService, rpc
from cliffracer_resilience import ResilienceExtension, rate_limit

class ApiService(CliffracerService):
    resilience = ResilienceExtension()

    @rpc
    @rate_limit(calls=10, window=60.0)
    async def heavy_operation(self, query: str) -> dict[str, str]:
        return {"result": f"processed {query}"}

    @rpc
    @rate_limit(calls=5, window=1.0, key=lambda ctx: ctx.payload.get("user_id", "anon"))
    async def user_operation(self, user_id: str) -> dict[str, str]:
        return {"user": user_id}
```

`@rate_limit` wraps the handler in a coroutine function, because taking a permit is awaited, so
decorating a synchronous function turns it into one that callers must `await`.

A limit is counted per service and per handler, partitioned by the key's value. The counter's
key is `<namespace>.<service>:<handler>:<value>` (`<service>:<handler>:<value>` with no
namespace, and `<service>:<handler>` for a handler that declares no key). Two handlers keyed on
one header therefore count a caller on their own, each against its own `calls`; two services
never share a counter, even in one `KvRateLimiter` bucket and with handlers of the same name; and
the replicas of one service, which share a limiter, do share it. A budget cannot be shared across
services. The names are not escaped: a service named `a.b` with no namespace and a service named `b`
in the namespace `a` have one scope, and share their counters.

A key partitions the limit per value. A string key reads a request header by
default, case-insensitively. This makes an authenticated identity authoritative:
a request body cannot shadow it. Use `key_source="payload"` when the payload is
deliberately the authority. A request that lacks the declared value is refused, naming
what is missing (an RPC is answered `refused`, and a durable event is acknowledged, since
redelivery cannot supply it), instead of silently falling into a shared bucket. It counts
among the handler's refusals in `/health`. A callable key receives the dispatch
context by default. Declare `key_source="payload"` to pass the handler payload
instead. Callable errors fail the check; they never select another source.

Either kind resolves against what the handler receives. For an event listener
that is the event's payload -- the envelope's `data` -- not the envelope around
it. For example, `key="user_id", key_source="payload"` and
`ctx.payload.get("user_id")` read the same field an RPC handler would.

A default limit applies one limit to every RPC and event handler that declares none:
`ResilienceExtension(default_calls=100, default_window=60.0)`. The two are given together, or
construction raises `ConfigurationError`, and they follow the same rules as `@rate_limit`:
`default_calls` is a whole number of at least 1 and `default_window` a finite number of seconds
above 0. Each handler counts under its own name, and a handler that declares `@rate_limit` keeps
its own limit. A `describe` request and a timer or cron firing are not limited by the default:
they have no caller to throttle, and a limit on `describe` would refuse a client that verifies the
contract. `/info` reports the default as `default_limit`.

A limit counts delivery attempts. Every dispatch the extension sees spends a permit and it has
no record of which message that is, so a JetStream redelivery of a message that was NAKed or
timed out spends another, and a permit spent on a message that a later extension then refuses
is not given back. A message the limit itself refuses spends none, and is redelivered later as
a first attempt. A failing durable handler is therefore throttled by its own retries.

A limit is checked before the payload is validated, because validation runs after every declared
extension. A payload that validation refuses has already spent a permit of the limit, and is
answered `validation failed` while the limit has one and `rate limit exceeded` once it has none. A
payload that cannot be decoded (a body that is not JSON or msgpack) is refused before any
extension runs and spends none.

Raw partition values may be credentials. Distributed KV key names, refusal
metadata and warning logs therefore carry only a SHA-256 fingerprint.

### 3. Distributed Rate Limiter with NATS KV

```python
from cliffracer import CliffracerService, rpc
from cliffracer_resilience import ResilienceExtension, KvRateLimiter, rate_limit

class DistributedService(CliffracerService):
    resilience = ResilienceExtension(limiter=KvRateLimiter(bucket_name="rate_limits"))

    @rpc
    @rate_limit(calls=100, window=60.0)
    async def shared_endpoint(self) -> str:
        return "success"
```

The extension opens the bucket under the service's subject prefix, as a `KvExtension`
bucket is named (`bucket_name="rate_limits"` under prefix `px` is `px_rate_limits` on the
broker), so two environments on one broker do not share counters. A limiter you open
yourself, with `init_kv(js=...)` or a `kv=` you pass, keeps the name you gave it.

A `KvRateLimiter` names one bucket, and a limiter given to a declared extension is shared by
every service built from it. Setting it up under a second, different subject prefix raises
`ConfigurationError` at start (the two would otherwise count in one bucket); give each
service its own limiter.

A limiter passed to `ResilienceExtension(limiter=...)` is shared, by identity, by every service
built from that declaration, without wrapping it in `SharedDependency`: `RateLimiter` copies
itself as itself, so one limiter is one budget across them, which is what a distributed limiter
is for. Leave `limiter` out and each service gets its own `InMemoryRateLimiter`. A custom limiter
class inherits that, so one that must not be shared has to override `__deepcopy__`.

A custom limiter subclasses `RateLimiter` and implements `acquire` and `reset`, and may override
`get_retry_after`, `get_retry_after_for` and `prune_expired`. A refusal tells the caller when to retry
from `get_retry_after_for(key, calls, window)`: when fewer than `calls` permits will be live, so a
call is admitted. `get_retry_after(key, window)` is a lower bound, the earliest a permit can leave the
window, and the two agree while a key holds no more permits than its limit (it can hold more after a
limit is lowered, or when replicas race on one bucket). The shipped limiters answer both exactly; a
custom limiter that overrides only `get_retry_after` is asked that for a refusal. It tells `/health` what it is by overriding `health_details()` to return a dict, which is
reported as it is under `resilience.rate_limiter` (the shipped limiters return `backend` and
`status`). One that does not is reported by its class name with the status `unreported`, which
claims nothing about whether it is distributed.

The distributed limiter does not grant work when its bucket is unavailable,
its stored value is invalid, or CAS contention exhausts the bounded retry
loop. Set `in_memory_fallback=True` only when availability is more important
than a cluster-wide bound. In that mode each process has an independent local
budget; `/health` reports `resilience.rate_limiter.status="degraded"` and a
fallback count until the distributed backend recovers. A limiter configured on
one handler appears under `resilience.handler_rate_limiters.<handler>`.

The bucket's timestamps are wall-clock times, since they are compared across processes (the
in-memory limiter uses a monotonic clock). The window is therefore as accurate as the replicas'
clocks agree: a replica whose clock is ahead expires the others' entries early, which widens the
limit by the skew, and a clock stepped backwards keeps entries alive for the length of the step.
The extension identifies event limits by handler, so namespaces, wildcard
subjects, validated listeners and broadcasts keep the same configured limiter.
Calling a second rate-limited operation from a dispatched handler evaluates
that operation's own limit independently.
That operation counts against its own `InMemoryRateLimiter` unless its decorator is given
`limiter=`: `ResilienceExtension(limiter=...)` reaches the dispatched handler, not a function
that is called directly, so a direct call's limit is per process.

The bucket holds one entry per partition key and does not remove an entry when its
window passes, so a key that a caller controls makes it grow. Bound it deliberately:
`KvRateLimiter(bucket_ttl=...)` sets a time-to-live when the limiter creates the bucket
(an existing bucket keeps its configuration), and it must be at least the longest window
any service using the bucket declares, or a limit is weakened; or call
`await limiter.prune_expired(window)` on a schedule to delete the entries whose timestamps
have all left that window. `await limiter.reset()` with no key clears every counter in the
bucket, including other services' if they share it; `reset(key)` clears one.

### 4. What the extension reports

`/health` carries, under `resilience`:

- `rate_limiter`: the limiter's backend and status (what its `health_details()` returns), with
  `tracked_keys` beside an in-memory limiter, and `handler_rate_limiters` for a handler that
  names its own;
- `rate_limits`: how many dispatches each handler's limit let through and refused since the
  service started (`by_handler`) and the totals. The figures grow with the handlers a service
  declares, never with the partition keys callers send. A payload that validation refuses
  counts in `permitted`, because the limit let it through first;
- `circuits`: for each `ResilientRpcProxy` the service declares, the `destination`, the `state`
  (`closed`, `open` or `half_open`), the `failure_count` and the `seconds_in_state`.

`/info` carries, under `resilience`, the `limits` by handler (`calls`, `window` and where the
key is read from: `header:<name>`, `payload:<name>`, `function` or `handler`, never a key
value), the limiter class, and the `default_limit` when one is set.
