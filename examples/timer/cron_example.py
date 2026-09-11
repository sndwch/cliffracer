#!/usr/bin/env python3
"""
Cron Scheduling Example for Cliffracer

Demonstrates the @cron decorator for wall-clock scheduling, alongside @timer
(fixed interval). Cron expressions are evaluated in UTC by default; pass tz to
evaluate in another timezone.

Run:
    python examples/timer/cron_example.py
(Requires a NATS server at nats://localhost:4222.)
"""

import asyncio
from datetime import UTC, datetime

from cliffracer_cron import cron

from cliffracer import CliffracerService, ServiceConfig, timer


class ScheduledJobsService(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="scheduled_jobs"))
        self.run_log: list[str] = []

    def _stamp(self, label: str) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.run_log.append(f"{now} {label}")
        self.logger.info(f"ran {label} at {now}")

    # Fixed interval — every 30 seconds.
    @timer(interval=30)
    async def heartbeat(self):
        self._stamp("heartbeat (every 30s)")

    # Cron — top of every minute (UTC). Good for seeing it fire quickly in a demo.
    @cron("* * * * *")
    async def every_minute(self):
        self._stamp("every_minute (cron '* * * * *' UTC)")

    # Cron — 09:00 every day, evaluated in Chicago local time.
    @cron("0 9 * * *", tz="America/Chicago")
    async def morning_report(self):
        self._stamp("morning_report (09:00 America/Chicago)")

    # Named schedule + eager: runs once on startup, then hourly.
    @cron("@hourly", eager=True)
    async def hourly_rollup(self):
        self._stamp("hourly_rollup (@hourly, eager)")


async def main():
    service = ScheduledJobsService()
    await service.start()
    print("Scheduled jobs running. @hourly fired once on startup (eager).")
    print("Watch for 'every_minute' at the top of each minute. Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()  # run until interrupted
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await service.stop()
        print(f"\nRan {len(service.run_log)} scheduled jobs:")
        for line in service.run_log:
            print(f"  {line}")


if __name__ == "__main__":
    asyncio.run(main())
