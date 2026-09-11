# cliffracer-metrics

Dispatch timing and counting for cliffracer services, plus batching and
connection-pooling helpers.

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer_metrics import MetricsExtension

class OrderService(CliffracerService):
    metrics = MetricsExtension()
```

`MetricsExtension` records a count, an error count and a latency window per
dispatch kind, and contributes them to `/health` under the attribute name you
declared it as. Declared as `metrics` above, its numbers appear under
`"metrics"`.

A refusal is counted separately from a handler error, so a service refusing
unauthenticated callers reports that as policy rather than as a thousand
crashes.

## A pool of connections

```python
from cliffracer import CliffracerService, rpc
from cliffracer_metrics import PoolExtension

class Ingest(CliffracerService):
    pool = PoolExtension(max_connections=8)

    @rpc
    async def bulk(self, subject: str) -> dict[str, bool]:
        reply = await self.pool.request(subject, b"{}")
        return {"ok": True}
```

`PoolExtension` builds an `OptimizedNATSConnection` from the service's own
config, credentials included, connects it when the service starts, closes it
when the service stops, and reports its connection count under `/health`.

The pool is a second set of connections the handler reaches for. The service's
own connection carries `call_rpc`, `publish_event` and every subscription, so
what goes through the pool is what a handler sends through it.

You construct `BatchProcessor` and `PerformanceMetrics` yourself. Each has a
package test that builds and exercises it, which is the working example of how
to use it.

Installed from PyPI, versioned in lockstep with `cliffracer`.
