from cliffracer.core import CliffracerService, ServiceConfig


class ForeignService(CliffracerService):
    """Defined here and imported by `imports_a_service`, so it is not that module's own."""

    def __init__(self):
        super().__init__(ServiceConfig(name="foreign_service"))
