"""A service that builds its config from a broker URL nats-py cannot use, and that carries a password."""

from cliffracer.core import CliffracerService, ServiceConfig

PASSWORD = "sup3rs3cretpassw0rd"

# A bad port, with a user and a password in front of the host.
REFUSED_URL = f"nats://user:{PASSWORD}@broker.example:notaport"


class BadlyConfiguredService(CliffracerService):
    """Takes the URL from a variable, as a service reading its environment does."""

    def __init__(self):
        url = REFUSED_URL
        super().__init__(ServiceConfig(name="bad_cfg", nats_url=url, health_port=0))
