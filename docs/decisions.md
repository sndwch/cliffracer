# Architecture Decision Records (ADRs)

Canonical architectural constraints and invariants governing the Cliffracer runtime.

## ADR-0001: Persistence Boundary
- **Status**: Accepted
- **Context**: Services frequently require database access, but coupling persistence drivers or ORMs to the core messaging framework creates bloat and imposes unwanted opinions.
- **Decision**: Cliffracer does not own persistence. There is no database driver, connection pool, repository abstraction, or migration tool in core.
- **Consequences**: Applications manage their own persistence layers and connection pools. Persistence additions to core are out of scope.

## ADR-0002: Core HTTP Endpoint Scope
- **Status**: Accepted
- **Context**: Orchestrators and probes require HTTP health checks, but pulling in full HTTP frameworks (FastAPI, Starlette) into core adds substantial dependency overhead.
- **Decision**: Core provides built-in HTTP serving exclusively for `GET /health` and `GET /info` using Python stdlib `asyncio`.
- **Consequences**: Services answer container/Kubernetes probes without requiring external web frameworks. All other HTTP routes and WebSockets belong in `cliffracer-http`.

## ADR-0003: Health Endpoint Status Code Semantics
- **Status**: Accepted
- **Context**: Container orchestrators and simple curl healthchecks rely on HTTP status codes to detect service liveness and readiness without parsing response bodies.
- **Decision**: `/health` returns HTTP 200 when healthy and HTTP 503 when unhealthy or when any active dependency probe fails.
- **Consequences**: Health status is immediately evaluable by `curl -f` and orchestrator probes without requiring JSON payload parsing.

## ADR-0004: Extension Composition Over Class Mixins
- **Status**: Accepted
- **Context**: Class inheritance mixins create complex Method Resolution Order (MRO) interactions, implicit state collisions, and rigid coupling.
- **Decision**: Optional features and capabilities are composed via extensions declared as class attributes and bound per service instance.
- **Consequences**: Extensions do not join the class MRO. Extension lifecycles are explicitly supervised by the container, ensuring clean encapsulation, independent packaging, and clear execution boundaries.

## ADR-0005: Extension Immutability and Instance Isolation
- **Status**: Accepted
- **Context**: Service class attribute definitions are shared across instances. Mutable state on declared attributes would cause state bleed across service instances in the same process.
- **Decision**: Extension attributes declared on service classes act as immutable factory specifications. `bind()` constructs a fresh runtime instance and isolates declaration arguments per service instance.
- **Consequences**: Runtime state is strictly isolated between service instances. Passing uncopyable objects or mutating extension specifications at runtime fails loudly.

## ADR-0006: Explicit Message Refusal Channel
- **Status**: Accepted
- **Context**: Pre-dispatch hook failures need differentiated handling between intentional policy rejections (e.g., authentication failures) and unintentional hook crashes.
- **Decision**: `RejectMessage` raised from `worker_setup` is the only hook exception that changes dispatch outcome. The handler is skipped and the message is acknowledged (or dead-lettered) rather than negatively acknowledged (NAK). All other hook exceptions are logged and swallowed.
- **Consequences**: Uncaught hook errors do not take down message dispatch. Policy refusals do not trigger redelivery storms and are tracked distinctly from internal errors.

## ADR-0007: Graceful Service Stop on Terminal Connection Loss
- **Status**: Accepted
- **Context**: Hard connection drops must terminate service consumption cleanly without hard-crashing the Python interpreter when multiple services are co-located in the same process.
- **Decision**: When a broker connection is permanently closed, the service triggers graceful wind-down via `await self.stop()`.
- **Consequences**: Co-located services sharing the Python process continue operating normally if unaffected.

## ADR-0008: Default Infinite Reconnection
- **Status**: Accepted
- **Context**: Ephemeral network partitions or broker restarts should not kill running application processes.
- **Decision**: `max_reconnect_attempts` defaults to `-1` (reconnect indefinitely).
- **Consequences**: Services remain alive during broker outages and resume operation automatically upon broker restoration. Terminal connection closing occurs only upon explicit administrative disconnection.

## ADR-0009: HTTP Package Decoupling
- **Status**: Accepted
- **Context**: Core NATS services operate strictly on message brokers. Forcing HTTP frameworks into core burdens messaging-only workers with unnecessary dependencies.
- **Decision**: HTTP endpoints, WebSockets, and FastAPI dependencies are isolated in the `cliffracer-http` distribution. Core runtime dependencies remain limited to `nats-py`, `pydantic`, and `loguru`.
- **Consequences**: Core services remain lightweight. Services requiring HTTP ingress explicitly opt in by installing `cliffracer-http`.

## ADR-0010: Fail-Fast Startup Validation Over Permissive Defaults
- **Status**: Accepted
- **Context**: Ambiguous service configurations or invalid handler signatures can easily cause silent runtime data loss or unrouted messages in production.
- **Decision**: When configuration or type contracts are ambiguous, Cliffracer fails loudly at startup with explicit errors rather than falling back to permissive defaults.
- **Consequences**: Misconfigurations are caught immediately at boot or test time rather than manifesting as silent runtime failures in production.

## ADR-0011: Structured Assertions Over Substring Matching
- **Status**: Accepted
- **Context**: Substring matching in validation, tests, and routing is fragile and prone to false positives caused by coincidental substring occurrences.
- **Decision**: All checks and verifications must parse structured payloads, inspect exact schema fields, or verify process exit codes rather than relying on unanchored substring matching.
- **Consequences**: Eliminates brittle tests and false positives in runtime payload inspection.

## ADR-0012: Bounded Initial Connection Timeout
- **Status**: Accepted
- **Context**: While infinite reconnection is desirable after establishing an initial connection, `nats-py` hangs indefinitely on initial dial when the broker is unreachable and `max_reconnect_attempts` is `-1`.
- **Decision**: `ServiceConfig.connect_timeout` enforces an explicit timeout (default 30s) on initial broker dial. If the broker is unreachable, startup raises `NatsError` once the timeout expires.
- **Consequences**: Services fail startup predictably within the configured timeout rather than hanging indefinitely.

## ADR-0013: Mandatory Event Delivery Semantics
- **Status**: Accepted
- **Context**: In NATS, omitting queue groups or durable consumer names causes implicit broadcast across all replicas. Developers often forget this, inadvertently converting competing worker queues into duplicate fanout.
- **Decision**: Every `@listener` must explicitly specify either `fanout=True` (ephemeral broadcast) or `durable="<name>"` (persistent queue/consumer). Omitting both raises `ConfigurationError` during handler discovery.
- **Consequences**: Prevents accidental replica event duplication in production by enforcing explicit operational intent at service definition time.

## ADR-0014: Non-Retryable Message Termination
- **Status**: Accepted
- **Context**: Retrying messages that failed deterministic schema validation or static authorization checks consumes broker resources, fills logs, and creates redelivery storms.
- **Decision**: Messages failing schema validation are routed to a Dead Letter Queue (DLQ) if configured and terminated (`term()`). Policy refusals (`RejectMessage`) acknowledge immediately.
- **Consequences**: Poison messages and unauthenticated requests are eliminated from the active stream immediately without redelivery loops.

## ADR-0015: Dispatch Context Isolation
- **Status**: Accepted
- **Context**: In asynchronous runtimes, task reuse and context variable inheritance can cause ambient trace or correlation IDs to leak between unrelated message dispatches.
- **Decision**: Every message dispatch initializes its own scoped `CorrelationContext` and resets the underlying `ContextVar` upon completion. Periodic timers reset the context before and after each execution.
- **Consequences**: Guarantees complete correlation isolation between dispatches; ambient IDs never leak across operations.

## ADR-0016: Active Dependency Health Probing
- **Status**: Accepted
- **Context**: Passive boolean health flags fail to detect hung connections, network deadlocks, or unresponsive downstream dependencies.
- **Decision**: Declared dependencies execute an active round-trip probe with a bounded timeout during health checks. A failed or timed-out probe returns HTTP 503. `health_host` defaults to `127.0.0.1` to keep probe endpoints private by default.
- **Consequences**: Health status reflects real-time end-to-end operational viability rather than cached or stale startup flags.

## ADR-0017: Service and Container Separation
- **Status**: Accepted
- **Context**: Isolating NATS connection management, extension hooks, and message dispatch from application-level service logic improves maintainability and testability.
- **Decision**: `Container` manages broker connectivity, extension lifecycles, and dispatch pipelines. `CliffracerService` serves as the user-facing service interface, providing clean delegation to its internal container.
- **Consequences**: Clear separation of concerns between user-facing service definition and runtime message dispatch infrastructure.
