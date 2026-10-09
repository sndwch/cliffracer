"""A service runner that must keep giving its event loop back while it watches a started service.

`python -m tests.fixtures.runner_loop_process` prints `DONE` once the runner, asked to stop by a
timer on its own loop, has returned. A runner that spins without yielding never lets the timer
fire, and nothing inside the frozen loop can time it out, so the test that runs this bounds it from
outside the process.
"""

import asyncio

from cliffracer import ServiceConfig
from cliffracer.runners.orchestrator import RUNNER_OK, ServiceRunner


class Steady:
    def __init__(self):
        self.config = ServiceConfig(name="steady", health_port=0)

    async def start(self):
        pass

    async def stop(self):
        pass


async def main():
    runner = ServiceRunner(Steady)
    asyncio.get_running_loop().call_later(0.3, runner._shutdown_event.set)
    assert await runner.run() == RUNNER_OK
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
