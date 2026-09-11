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
from cliffracer_metrics import BatchProcessor

class AnalyticsService(CliffracerService):
    def __init__(self, config):
        super().__init__(config)
        # Process in batches of 100, or every 5 seconds
        self.processor = BatchProcessor(self._process_batch, batch_size=100, timeout=5.0)

    async def start(self):
        await super().start()
        await self.processor.start()

    async def stop(self):
        await self.processor.stop()
        await super().stop()

    @listener("analytics.track", fanout=True)
    async def track_event(self, subject: str, **data):
        # Queue the item; returns immediately
        await self.processor.add_item(data)

    async def _process_batch(self, items: list[dict]):
        # Write 100 items to the database in a single query
        await self.db.bulk_insert(items)
```

## OptimizedNATSConnection Pool

When outward RPC volume warrants multiple underlying connections, `OptimizedNATSConnection` manages a pool of NATS client instances. It health-checks and rotates connections across the pool.

```python
from cliffracer_metrics import OptimizedNATSConnection

class HeavyService(CliffracerService):
    def __init__(self, config):
        super().__init__(config)
        self.pool = OptimizedNATSConnection(self, max_connections=10)
        
    async def start(self):
        await super().start()
        await self.pool.start()
        
    async def stop(self):
        await self.pool.stop()
        await super().stop()
```

The pool correctly propagates your `ServiceConfig` credentials to every connection it opens, and gracefully handles backoff and reconnection logic independently of the core service lifecycle.
