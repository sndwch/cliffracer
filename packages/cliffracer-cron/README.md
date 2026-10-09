# cliffracer-cron

Wall-clock `@cron` scheduling for cliffracer services, on `croniter`.

Core ships `@timer` for fixed intervals. This package adds cron expressions and
timezones for schedules that must land at a wall-clock time.

```python
from cliffracer import CliffracerService
from cliffracer_cron import cron

class Reports(CliffracerService):
    @cron("0 9 * * *", tz="America/Chicago")
    async def daily_summary(self) -> None: ...
```

`@cron` sets the same marker `@timer` does, so core discovers these handlers
without knowing this package exists.

Timezone schedules use wall-clock cron fields and elapsed-time waits. A clock
change therefore does not make a job run an hour early or late. During a
fall-back transition, two matching local times are distinct occurrences and
run at their distinct instants; they are never fired back-to-back. A local time
skipped by a spring-forward transition follows `croniter`'s next valid
occurrence.

`@cron(..., distributed=True)` runs a schedule once across replicas, taking its
locks in a Key-Value bucket (`bucket=`, default `cron_locks`). The service declares
a `cliffracer-kv` `KvExtension` under any attribute name; the timer finds it by
type. A service that declares several uses the one named `kv`, and one that
declares several and none named `kv` is refused at startup. A service with none is
refused at startup too.

Each firing leaves a record in the bucket, under
`cron.<namespace>.<service>.<method>.<epoch>` (no namespace segment when the service has
none), which is also what makes one replica run it: the replica that creates the record
runs the job, and a peer that finds it skips the interval. The record holds `service`, `method`,
`replica`, `target_time`, `started_at`, and once the run ends `completed_at`,
`duration_ms` and a `status`: `completed`; `failed`, with the `error`; `refused`, with the
`refusal`, when an extension refused the firing; or `cancelled`, when a stop cancelled
the handler after its grace ran out (the cancellation is raised after the record is
written; a handler that finishes within the grace is `completed`). A record is kept, so an interval that was cancelled is not run again by a peer
or by the replica after a restart. `replica` is the service's `instance_id` attribute if
it sets one, else `<hostname>-<pid>`, which in a container names the pod and is the same
on every firing.

`lease_ttl` (default 300 seconds) is how long a running lease is honoured before another run
starts over it, and `no_overlap` (default true) holds one while a run is going. A `lease_ttl` that
is not a finite number of seconds above zero, a `no_overlap` that is not a bool and a `bucket` the
Key-Value layer would refuse raise `ConfigurationError` where the job is declared. The bucket
expires every key in it, the leases and the interval records included, after its own TTL, which is
fixed by whichever job opened the bucket first (a new bucket is given `max(lease_ttl, 300)`
seconds; the bucket is opened when the timer starts). A job with `no_overlap` whose `lease_ttl` is
longer than the TTL of the bucket it is handed is refused when it starts, with a `ConfigurationError` that names the
job, its `lease_ttl`, the bucket and the bucket's TTL; a bucket with a longer TTL, or none,
is used as it is. Give a job with a long lease a bucket of its own, or start the job with
the longest lease first.

`deadline=` bounds each firing as `@timer`'s does: a firing still running at it is cancelled and
counted as an error, and the calls it makes wait at most what is left. A job with `no_overlap`
refuses a `deadline` longer than its `lease_ttl` where it is declared, with a `ConfigurationError`.

Installed from PyPI, versioned in lockstep with `cliffracer`.
