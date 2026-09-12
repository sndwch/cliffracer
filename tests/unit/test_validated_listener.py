"""@validated_listener metadata and ServiceConfig defaults."""

import pytest
from pydantic import BaseModel

from cliffracer import ServiceConfig, validated_listener

pytestmark = pytest.mark.unit


class Evt(BaseModel):
    id: str


def test_decorator_attaches_metadata():
    @validated_listener("orders.created", Evt, fanout=True)
    async def handler(self, message: Evt):
        pass

    assert hasattr(handler, "_cliffracer_validated_events")
    pattern, schema, on_invalid = handler._cliffracer_validated_events[0]
    assert pattern == "orders.created"
    assert schema is Evt
    assert on_invalid is None


def test_decorator_on_invalid_override():
    @validated_listener("orders.x", Evt, on_invalid="drop", fanout=True)
    async def handler(self, message: Evt):
        pass

    assert handler._cliffracer_validated_events[0][2] == "drop"


def test_service_config_invalid_defaults():
    cfg = ServiceConfig(name="svc")
    assert cfg.default_on_invalid == "deadletter"
    assert cfg.dlq_subject == "dlq.{service}"
