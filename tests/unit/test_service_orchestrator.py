import pytest

from cliffracer.core import ServiceConfig
from cliffracer.runners.orchestrator import ServiceOrchestrator


class DummyService:
    def __init__(self):
        self.config = ServiceConfig(name="dummy")


@pytest.mark.unit
def test_add_service_with_overrides_creates_runner():
    orch = ServiceOrchestrator()
    orch.add_service(DummyService, overrides={"nats_url": "nats://x:4222"})
    assert len(orch.runners) == 1
    assert orch.runners[0].overrides == {"nats_url": "nats://x:4222"}


@pytest.mark.unit
def test_add_service_back_compat_with_config_positional():
    orch = ServiceOrchestrator()
    cfg = ServiceConfig(name="dummy", auto_restart=False)
    orch.add_service(DummyService, cfg)
    assert len(orch.runners) == 1
    assert orch.runners[0].config is cfg
