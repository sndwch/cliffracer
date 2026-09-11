# Timer examples

`@timer` runs a method on a fixed interval. `@cron` runs one on a wall-clock
schedule.

## `@timer`

```python
from cliffracer import CliffracerService, timer

class MyService(CliffracerService):
    @timer(interval=30)
    async def health_check(self):
        await self.check_database_connection()

    @timer(interval=60, eager=True)
    async def collect_metrics(self):
        metrics = self.get_performance_data()
        await self.publish_event("metrics.collected", data=metrics)
```

| option | default | |
|---|---|---|
| `interval` | required | seconds between runs |
| `eager` | `False` | also run once when the service starts |
| `max_drift` | `1.0` | seconds of lateness tolerated before it logs a drift warning |
| `error_backoff` | `5.0` | seconds to wait after a failed run |

Async and sync methods both work. The timer awaits a coroutine function and
calls a plain one.

A run that raises is logged, counted in `error_count`, and followed by an
`error_backoff` pause. The timer keeps running.

Timers run as separate tasks, so a slow timer method delays its own next run
rather than the other timers.

## `@cron`

`@cron` comes from the `cliffracer-cron` package.

```python
from cliffracer import CliffracerService
from cliffracer_cron import cron

class MyService(CliffracerService):
    @cron("0 9 * * *")
    async def daily_report(self):
        ...

    @cron("*/15 * * * *", tz="America/Chicago")
    async def sync(self):
        ...

    @cron("@hourly", eager=True)
    async def rollup(self):
        ...
```

`expression` takes a cron string or a named schedule such as `@daily`. `tz` is
an IANA timezone name and defaults to `"UTC"`. `eager` behaves as it does on
`@timer`.

An invalid expression or timezone raises `ValueError` at decoration time, so a
typo fails when the module is imported.

Both decorators schedule the next run from the current time. See
`cron_example.py`.

## Statistics

```python
stats = service.get_timer_stats()

for timer_info in stats["timers"]:
    print(f"{timer_info['method_name']}: {timer_info['execution_count']} executions")
```

`get_timer_stats()` returns `timer_count` and a `timers` list. Each entry
carries `method_name`, `interval`, `eager`, `is_running`, `execution_count`,
`error_count`, `last_execution_time`, `average_execution_time`,
`total_execution_time` and `error_rate`, where `error_rate` is a percentage.

## Running the examples

```bash
cd examples/timer
python timer_service_example.py
```

`timer_service_example.py` runs five timers at different intervals.
`timer_with_metrics.py` adds `MetricsExtension`. `cron_example.py` covers the
cron schedules above.
