# Cliffracer Architecture

How the pieces fit together: one service class, extensions declared as class
attributes, and NATS underneath. For the API itself see
[api-reference.md](api-reference.md); for writing an extension see
[extensions.md](extensions.md).

## The service

There is one service class, `CliffracerService`. It is what you declare handlers
and extensions on, and it hands the rest to the `Container` it holds: the
container owns the NATS connection, the extension lifecycles and the dispatch
chain. The service discovers decorated handlers at `start()` and serves
`GET /live`, `GET /ready`, `GET /health` and `GET /info` on its own listener.

Everything else is an extension.

## Extensions

Optional functionality is composed by **declaring extensions as class
attributes**.

```python
from cliffracer import CliffracerService
from cliffracer_logging import LoggingExtension
from cliffracer_metrics import MetricsExtension

class MyService(CliffracerService):
    """Service with structured logging and metrics"""

    logging = LoggingExtension()
    metrics = MetricsExtension()
```

The attribute name is how you reach the extension (`self.metrics`). Each
extension ships as its own distribution:

| distribution | provides |
|---|---|
| `cliffracer-auth` | JWT auth, roles and permissions |
| `cliffracer-logging` | structured logging, correlation logging, log-to-NATS |
| `cliffracer-metrics` | in-process counters over the dispatch hooks |
| `cliffracer-otel` | OpenTelemetry distributed tracing and W3C context propagation |
| `cliffracer-kv` | NATS JetStream Key-Value and Object Store integration with bucket TTL |
| `cliffracer-resilience` | circuit breaking, sliding-window rate limiting, and resilient RPC |
| `cliffracer-cron` | `@cron` handlers (a `Timer` subclass, so core's timer discovery runs it — no extension to declare) |
| `cliffracer-dlq` | `cliffracer-dlq`, a read-only command that lists, shows and counts the dead letters in their stream — no extension to declare |
| `cliffracer-cyanide` | fault injection for testing: delay, raised faults, simulated timeouts and dropped replies |

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
through `CorrelationContext.get()`. `cliffracer-logging` puts it on log lines.

## Health

Every service serves four routes, and core does it without a web framework:

| Route | Answers | Status code |
|---|---|---|
| `GET /live` | whether the process is running | 200 while running, 503 once stopped |
| `GET /ready` | whether the service can do its work: broker connected, declared dependencies passing | 200 when healthy, 503 otherwise |
| `GET /health` | the same as `/ready` | the same |
| `GET /info` | the service descriptor | 200 |

`/live` consults neither the broker nor any dependency, so it stays 200 through a
broker outage. That is what makes it the route for a liveness probe: a
`livenessProbe` pointed at `/health` or `/ready` has the orchestrator restart the
pod whenever the broker or a dependency is down for longer than its failure
threshold, and a restart does not bring either back. Point the readiness probe
at `/ready`, which takes the pod out of rotation without restarting it.

```text
GET /ready -> {
  "service": "user_service",
  "status": "healthy",          # healthy | unhealthy | connecting | disconnected | stopped
  "timestamp": "2026-09-06T18:04:11.512Z",
  "nats_connected": true,
  "nats_rtt_ms": 0.31,          # the last broker round trip; null when none was measured
  "features": {...},            # handler counts by kind
  "dead_letters_lost": 0        # dead letters that could not be published since start
}
```

The status code carries the same answer as the field: 200 when `status` is
`healthy`, 503 in every other state. A probe that reads only the code is enough.

`status` is the field to probe, and its vocabulary is five values, not two:
`connecting` means broker connection dial is in progress, `disconnected` means
the broker connection is closed for good, and `stopped` means before `start()`
or after `stop()`. Declared dependencies add a
`dependencies` block, and `unhealthy_dependencies` names the failures so a
probe reading one line does not have to walk it.

The broker is asked, not only read. While nats-py's flag says connected, `/ready` and `/health`
send the broker a round trip, a PING and its PONG on the existing connection, bounded by
`broker_probe_timeout` (2 seconds by default). A round trip that fails or takes longer makes the
status `disconnected` and `nats_connected` false, so a connection that has gone silent without being
reset, as when a partition drops packets, is reported within that bound instead of when nats-py's
ping loop gives up: between `max_outstanding_pings * ping_interval` and
`(max_outstanding_pings + 1) * ping_interval` seconds after the partition begins, which is 240 to 360
seconds at nats-py's defaults of 120 seconds and 2. `nats_rtt_ms` carries the last round trip's
time, and is `null` when none was measured. The result is reused for `broker_probe_cache` seconds
(1 by default), a failure included, and requests that arrive while a round trip is in flight share
it, so a burst of probes costs one PING. `broker_probe_timeout: None` turns the round trip off and
`/ready` reads nats-py's flag alone. `ServiceConfig.ping_interval` and
`ServiceConfig.max_outstanding_pings` still decide when nats-py itself starts to reconnect; 5
seconds and 2 narrow it to 10 to 15 seconds, at the cost of one ping on each connection every 5
seconds.

Three things can make readiness go down, or go down and come back, while the broker is fine:

- A service whose own event loop is blocked for longer than the bound makes the round trip late, so
  readiness reports `disconnected`. The health listener shares that loop and is stalled by the same
  block.
- Many replicas probing one broker at once, after a broker restart for example, each send one PING
  per `broker_probe_cache` seconds at most, so the load on the broker grows with the replica count
  and shrinks with a longer cache.
- An orchestrator's failure and success thresholds decide what readiness going down and returning
  does to a pod. A `failureThreshold` above 1 keeps one missed round trip from taking a pod out of
  rotation.

`dead_letters_lost` counts the dead letters the service could not publish, from an invalid,
undecodable or exhausted message, since it started. It never changes `status`: the delivery is
terminated either way. Alarm on the number rising, and read the payload from the error log line
that names the cause.

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

Settings come from the environment under `CLIFFRACER_`: `CLIFFRACER_SUBJECT_PREFIX` for core,
`CLIFFRACER_NATS_URL` for the client generator and for `cliffracer describe` and `cliffracer call`,
`CLIFFRACER_LOG_DIR` for `cliffracer-logging` and `CLIFFRACER_CYANIDE_*` for `cliffracer-cyanide`; the
`cliffracer-dlq` command, `cliffracer describe` and `cliffracer call` take `NATS_URL`, `NATS_CREDS`,
`NATS_USER`, `NATS_PASSWORD` and `NATS_TOKEN` as the defaults of their connection flags.
The README lists them in one table. `AuthConfig` takes its `secret_key` as an argument.

## Testing

Every test module declares one tier marker, once, at module level. The tier is
the directory it sits in:

| | | |
|---|---|---|
| `tests/unit/` | `unit` | the library, in process |
| `tests/transport/` | `unit` | the library over an in-memory transport |
| `tests/integration/` | `integration` | against a live broker |
| `tests/benchmark/` | `benchmark` | timings, not a correctness gate |
| `tests/repo/` | `repo` | the repository: docs, packaging, CI, the suite |
| `packages/*/tests/` | `unit` | an extension, alongside its own package |

`nats_required` and `slow` are orthogonal flags and may appear under any tier.
`$CLIFFRACER_TEST_NATS_URL` names the broker the whole suite dials, and a broker is
used only when it is named: without it nothing is dialled, not even to probe the
default address, so a run never reaches a broker someone else is using. The tests
marked `nats_required` skip, with the reason printed, and the rest run; that marker is on
every test of `tests/integration/`, which `tests/repo/test_every_broker_test_carries_the_marker.py`
keeps true. CI, `scripts/check_kv_compatibility.py` and a disposable
broker on ephemeral ports all set it. `load-testing/` contains locust scripts,
which the suite does not run.
`$CLIFFRACER_TEST_NATS_MONITOR_URL` selects that broker's HTTP monitoring base URL
for connection-leak checks. Unset, it is `http://localhost:8222` when the broker
under test is the default address, `nats://localhost:4222`; for any other broker
URL the check fails, naming the variable, because that address is another broker's
monitor. Set
both URLs when using a disposable broker on ephemeral ports. The monitor must be
reachable; its check fails instead of skipping.

`tests/repo/test_the_suite_follows_its_conventions.py` enforces the tier rule
and the two filename rules in CONTRIBUTING.md.

## Deployment

```dockerfile
FROM python:3.12-slim

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
        livenessProbe:
          httpGet:
            path: /live
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 10
          timeoutSeconds: 3
```

kubelet reaches the pod's IP rather than its loopback, so a service probed this
way sets `health_host="0.0.0.0"`.

`timeoutSeconds` on the readiness probe is above the slowest declared dependency
probe. Dependencies run concurrently and each is bounded by its own timeout, 2
seconds by default, so `/ready` takes as long as the slowest one when a
dependency hangs; kubelet's default `timeoutSeconds` is 1, which fails the probe
before the response, and the body naming the failed dependency, arrives.

A service that cannot reach its broker at startup logs the address and raises
`NatsError` once `connect_timeout` expires.
