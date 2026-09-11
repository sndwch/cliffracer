from cliffracer.core import CliffracerService, ServiceConfig


class AlphaService(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="alpha_service"))


class BetaService(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="beta_service"))


class NotAService:  # must be ignored by bare-module discovery
    pass
