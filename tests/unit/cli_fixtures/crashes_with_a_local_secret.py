"""A service whose start fails with a credential in a local variable of the frame that fails."""

from cliffracer.core import CliffracerService, ServiceConfig

PASSWORD = "FRAMEVALUE-CANARY-7c2e"


class Crashes(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="crasher", health_port=0, auto_restart=False))

    async def start(self):
        password = PASSWORD
        raise RuntimeError(len(password))
