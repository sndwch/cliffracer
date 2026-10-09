"""A JetStream pull loop whose fetch fails without ever suspending.

`python -m tests.fixtures.pull_loop_process` prints `DONE <attempts>` once the loop has run for a
moment and been cancelled. A fetch on a subscription that is already gone can raise before it awaits
anything, and then the loop's wait between attempts is the only point at which it gives its event
loop back. A loop that spins without it never reaches the cancel, and nothing inside the frozen
loop can time it out, so the test that runs this bounds it from outside the process.
"""

import asyncio
from unittest.mock import MagicMock

from nats.js.errors import NotFoundError

from cliffracer import CliffracerService, ServiceConfig


async def failing_fetch_loop() -> int:
    service = CliffracerService(
        ServiceConfig(name="pinger", health_port=0, jetstream_nak_backoff=0.0)
    )
    dispatcher = service.container.dispatcher.jetstream
    attempts = 0

    async def fetch(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise NotFoundError()

    sub = MagicMock()
    sub.fetch = fetch
    loop = asyncio.create_task(dispatcher.pull_loop(sub, "pinger"))
    await asyncio.sleep(0.2)
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    return attempts


if __name__ == "__main__":
    print(f"DONE {asyncio.run(failing_fetch_loop())}", flush=True)
