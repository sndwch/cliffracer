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
- **Cooldown & Probing**: After `recovery_timeout`, transitions to `HALF_OPEN` to permit a probe request. Success resets to `CLOSED`; failure trips back to `OPEN`.
- **Integration**: Replaces standard `RpcProxy` with `ResilientRpcProxy`.

### Rate Limiting
- **`@rate_limit(calls=100, window=60)` Decorator**: Declare call limits per time window directly on RPC and event handlers.
- **Dual Backends**:
  - `InMemoryRateLimiter`: Local sliding window using timestamp queues with concurrency locks.
  - `KvRateLimiter`: Distributed sliding window using NATS JetStream KV store with optimistic concurrency and automatic in-memory fallback.
- **Wire Refusal**: Excess calls raise `RateLimitExceeded(RejectMessage)` which the Cliffracer container translates to wire error `{"error": "refused: rate limit exceeded"}`, surfacing as `RpcRefused` on the client.
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
