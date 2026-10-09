from cliffracer.core import CliffracerService


class ConstructorRaises(CliffracerService):
    def __init__(self):
        raise RuntimeError("config missing: DATABASE_URL")
