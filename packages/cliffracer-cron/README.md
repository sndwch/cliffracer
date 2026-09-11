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

Installed from PyPI, versioned in lockstep with `cliffracer`.
