from cliffracer.core import CliffracerService, ServiceConfig
from tests.unit.cli_fixtures.foreign_service import ForeignService  # noqa: F401


class OwnService(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="own_service"))
