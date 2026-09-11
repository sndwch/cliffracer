# Philosophy & Landscape

Cliffracer is a strongly opinionated, async-native Python framework for building typed NATS services. It favors explicit messaging semantics and operational correctness over broker portability and framework magic.

Cliffracer trades generic portability for strict operational control and the full native power of the broker.


## The Ecosystem Gap

Python provides mature HTTP frameworks (FastAPI, Litestar), task queues (Celery, ARQ), and stream platforms (Kafka). Each system has a center of gravity:

* **HTTP frameworks** optimize around routes and request/response at the network edge.
* **Task queues** optimize around deferred background work.
* **Stream platforms** optimize around durable event logs.

Cliffracer instead treats low-latency service messaging, request-reply RPC, and durable events as first-class application primitives.

## Inspirations

The design of Cliffracer draws from concepts in several frameworks:

*   **Nameko (`nameko.io`):** Class-based service grouping where capabilities (extensions) are declared as attributes. Cliffracer serves as a spiritual successor rebuilt on native Python `asyncio`.
*   **Lightbus:** The explicit separation of RPC (commands) and Events (bus)—one shared infrastructure, but distinct semantics and delivery guarantees.
*   **NestJS:** A structured, decorator-driven architecture that treats message payloads and schemas as the primary interface contract.
*   **Moleculer:** Built-in NATS-native service mesh concerns, including load-balanced replicas, circuit breakers, metrics, and health probes.
*   **Zero (`Ananto30/zero`):** Handler signatures serve as the contract, allowing typed client SDKs to be generated directly from the live service.

## Why NATS?

NATS provides messaging primitives well-suited for service architectures:

*   **Subject-based addressing:** Service discovery and routing handled through subject names.
*   **Queue groups:** Automatic load-balanced request delivery across service replicas.
*   **Request-reply:** Correlation and inbox-based reply mechanisms built into the protocol.
*   **JetStream:** Stream persistence, at-least-once delivery, and consumer ack tracking.

The `nats-py` library provides the transport client. Cliffracer wraps it with typed dispatch, lifecycle management, and extension composition.

## Design Tenets

1.  **Explicit Over Permissive:** When multiple interpretations have materially different operational consequences, Cliffracer requires the developer to choose rather than applying a convenient default.
2.  **Message-First:** Services communicate via NATS subjects. HTTP ingress is handled by `cliffracer-http` as an edge protocol.
3.  **Opt-in Extensions:** Capabilities such as authentication, HTTP routing, metrics, and logging are packaged and loaded as separate extensions.
4.  **Typed Contracts:** Handler parameters and returns are validated against Pydantic types, and typed client SDKs are generated directly from handler signatures.
5.  **Operational Resilience:** Explicit timeouts, bounded startup connections, queue draining on shutdown, and fail-at-startup validation for ambiguous configurations.

## Broker-Native vs. Broker-Agnostic

Generic messaging frameworks abstract multiple brokers (Kafka, RabbitMQ, Redis, NATS) behind a unified interface to prioritize portability. FastStream is the strongest broker-agnostic option; choose it if you might not stay on NATS.

Cliffracer rejects broker portability. It is strictly NATS-native, for two reasons:

1. **Explicit Failure Semantics:** Abstractions hide the network. Cliffracer exposes JetStream-specific failure modes natively. Unhandled Python exceptions map to JetStream `NAK` with backoff; explicit `RejectMessage` exceptions map to `TERM` (dead-letter queue). A portability layer flattening these into generic ack/nack semantics obscures operational control.
2. **The Entire Capabilities Set:** A portability layer can only offer what every broker shares—the lowest common denominator. Being NATS-native allows Cliffracer to expose JetStream KV, NATS request-reply, queue-group replica semantics, and subject wildcards as first-class primitives.

The cost is that leaving NATS means rewriting the messaging layer; the bet is that you won't want to.

