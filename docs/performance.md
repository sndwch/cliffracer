# Performance & Metrics

The `cliffracer-metrics` distribution provides tools for observing dispatch latency, batching operations, and pooling broker connections.

## MetricsExtension

The `MetricsExtension` collects in-process counters over the dispatch hooks. It tracks calls, errors, and refusals across your service.

```python
from cliffracer import CliffracerService
from cliffracer_metrics import MetricsExtension

class MyService(CliffracerService):
    metrics = MetricsExtension()
```

When installed, it automatically contributes these statistics to the service's `GET /health` and `GET /info` endpoints, allowing orchestrators and monitoring tools to scrape real-time throughput data.

## BatchProcessor

When dealing with high-volume events (e.g., writing logs to a database, sending analytics, or bulk-updating records), processing each event individually can overwhelm downstream dependencies.

The `BatchProcessor` allows you to queue items and process them in bulk based on a configurable batch size or a time window, whichever is reached first. It safely flushes pending batches during service shutdown.

```python
from pydantic import BaseModel

from cliffracer_metrics import BatchProcessor

class TrackEvent(BaseModel):
    name: str
    value: float

class AnalyticsService(CliffracerService):
    def __init__(self, config):
        super().__init__(config)
        # A batch goes out at 100 items, or after 5 seconds, whichever is first.
        self.processor = BatchProcessor(batch_size=100, batch_timeout_ms=5000)

    async def stop(self):
        await self.processor.shutdown()
        await super().stop()

    @listener("analytics.track", fanout=True)
    async def track_event(self, subject: str, event: TrackEvent) -> None:
        # Returns once this item's batch has been processed. `results="shared"`, the default,
        # hands every caller the processor's return value; pass `results="per_item"` to give
        # each its own element of a returned list.
        await self.processor.add_item("analytics", event, self._write_batch)

    async def _write_batch(self, items: list[TrackEvent]):
        # Write the whole batch to the database in a single query.
        await self.db.bulk_insert(items)
```

## Connection pool

When outward request volume warrants several connections, `PoolExtension` keeps a pool of NATS clients (`OptimizedNATSConnection`) beside the service's own connection, and a handler sends through it:

```python
from cliffracer import CliffracerService, rpc
from cliffracer_metrics import PoolExtension

class HeavyService(CliffracerService):
    pool = PoolExtension(max_connections=8)

    @rpc
    async def bulk(self, subject: str) -> dict[str, bool]:
        await self.pool.request(subject, b"{}")
        return {"ok": True}
```

The extension builds the pool from the service's own config (credentials, inbox prefix, connect timeout, reconnect policy and ping interval and outstanding-ping limit; each connection is named `<service>-pool-<n>`), connects it when the service starts and closes it when the service stops. `request` and `publish` take the next connection in round-robin order. The service's own connection still carries `call_rpc`, `publish_event` and every subscription; only what a handler sends through the pool uses it.

A client that has closed for good, because the reconnect attempts above ran out, is skipped when the next connection is chosen, and logged at WARNING when nats-py closed it (a `close()` of the pool is silent); when every client is closed `request` and `publish` raise a `RuntimeError` saying so, and the pool is reconnected by `close()` and `connect()`. A second `connect()` while the pool holds connections does nothing, and two tasks calling it at once open one pool, the second waiting for the first. A `connect()` that fails, is cancelled or is cut off by the caller's timeout closes the connections it had opened and leaves the pool empty, so calling it again connects the whole pool. `/health` reports `connections` (how many the pool holds), `active_connections` and `closed_connections` (how many of them are connected and how many have closed for good), `connected` (whether any of the pool's own connections is connected) and `service_connected` (the owning service's own connection to the broker, which is not folded into `connected`: a pool whose sockets are up reads `connected` while its service is cut off). The pool's `get_stats()` reports the same split, `active_connections` from its own sockets and `service_connected` beside it, and also counts the connections closed for good and the errors nats-py reported.
