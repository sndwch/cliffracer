"""Wall-clock scheduling for cliffracer services.

`@cron` sets the same `_cliffracer_timers` marker `@timer` does, so core's
handler discovery needs no knowledge of this package -- installing it and
importing `cron` is the whole integration.

    from cliffracer import CliffracerService
    from cliffracer_cron import cron

    class Reports(CliffracerService):
        @cron("0 9 * * *", tz="America/Chicago")
        async def daily(self) -> None: ...
"""

from cliffracer_cron.cron import CronTimer, cron
from cliffracer_cron.distributed import DistributedCronTimer

__all__ = ["CronTimer", "DistributedCronTimer", "cron"]
