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

A refusal an extension authored is counted separately from a handler error, so a service
refusing unauthenticated callers reports that as policy rather than as a thousand crashes.
A gate that fails, such as an auth backend that is down or a validator that raises, is the
service breaking and not a caller being turned away: the reply says `internal`, and it is
counted as an error. A cancelled dispatch (a shutdown cancelling in-flight handlers, a
timeout cancelling one) is counted as `cancelled` and not as an error. The
counts are per dispatch kind (`rpc`, `async_rpc`, `event`, `timer`), not per
handler.

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
config, connects it when the service starts, closes it when the service stops,
and reports under `/health` how many connections it holds (`connections`), how many of them are
connected (`active_connections`) and closed for good (`closed_connections`), `connected` (whether
any of its own connections is) and `service_connected` (the service's own connection to the broker, kept
apart from `connected`). Each pooled connection takes
what a connection takes from `ServiceConfig` through
`ServiceConfig.nats_connect_kwargs()`, the one place the service's own
connection reads it too: the credentials and `nats_inbox_prefix`, so a pooled
request is answered where the service's own would be. It also takes the
service's `connect_timeout`, which bounds each connection, and the service's
`max_reconnect_attempts` and `reconnect_time_wait` unless the extension is
given its own. On the broker each connection is named `<service>-pool-<n>`.
The pool lives in this distribution beside `MetricsExtension`, and a service
that wants a pool installs it. A connection that nats-py closes for good,
because its reconnect attempts ran out, is logged at WARNING and counted
(`closed_connections` in `get_stats()`) and is skipped when the next connection is chosen; the
errors nats-py reports for a pooled connection are logged at ERROR and counted
(`connection_errors`). The disconnects and reconnects before a permanent close
are not logged; the service's own connection logs them.

The pool is a second set of connections the handler reaches for. The service's
own connection carries `call_rpc`, `publish_event` and every subscription, so
what goes through the pool is what a handler sends through it.

`PerformanceMetrics.get_latency_stats()` reports `p95_ms` and `p99_ms` as nearest-rank
percentiles: of 100 samples, p95 is the 95th smallest. `check_performance_targets()`
judges the latency target on p95.

`BatchProcessor.add_item(key, item, processor, results=...)` takes what its caller receives
from `results`, and never guesses it from what the processor returned. With `"shared"`, the
default, every caller gets the processor's return value as it is, whatever its type. With
`"per_item"` each caller gets its own element: the processor must return a list or tuple with
one result for each item in its call, and a batch whose processor returns anything else fails
every one of its callers with a `ValueError`. Items added with the same processor but different
`results` are processed in separate calls.

You construct `BatchProcessor` and `PerformanceMetrics` yourself. Each has a
package test that builds and exercises it, which is the working example of how
to use it.

`PerformanceMetrics.record_connection_event()` counts `total_connections`,
`failed_connections` and `reconnections`, and `connection_opened` and
`connection_closed` move `active_connections`, which is how many are open now.
`set_active_connections(n)` sets it outright. An event name it does not know raises
`ValueError`.

Installed from PyPI, versioned in lockstep with `cliffracer`.
