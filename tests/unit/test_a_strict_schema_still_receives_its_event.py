"""A listener whose model forbids extra fields still receives an event carrying a correlation id.

A publisher may put `correlation_id` among an event's fields, flat or inside an
envelope's `data`. Dispatch removes it before building the listener's model,
unless the model declares the field. A model with `extra="forbid"` is an
ordinary choice for a strict event contract, and without that removal every
such event would fail validation and be dead-lettered instead of delivered.
A model with the default `extra="ignore"` drops the key either way, so only a
forbidding model shows whether the removal happens.
"""

import json
from datetime import datetime

import pytest
from pydantic import BaseModel, ConfigDict

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.container import DispatchOutcome
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

SUBJECT = "items.created"


class StrictItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    price: float


class StrictItemWithId(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    price: float
    correlation_id: str | None = None


FIELDS = {"name": "gadget", "price": 25.5, "correlation_id": "corr-published"}

SHAPES = {
    "flat": FIELDS,
    "envelope": {
        "data": FIELDS,
        "timestamp": datetime(2026, 1, 2, 3, 4, 5).isoformat(),
        "source_service": "producer",
        "correlation_id": "corr-published",
    },
}


def _message(body: dict) -> MockMessage:
    return MockMessage(
        subject=SUBJECT,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


async def _dispatch(service_class, body: dict):
    svc = service_class(ServiceConfig(name="consumer"))
    svc._discover_handlers()
    outcome = await svc.container._dispatch_event(_message(body), pattern=SUBJECT)
    return outcome, svc.received


@pytest.mark.parametrize("shape", SHAPES.values(), ids=SHAPES.keys())
async def test_a_validated_listener_with_a_forbidding_schema_receives_the_event(shape):
    class Consumer(CliffracerService):
        received: list = []

        @validated_listener(SUBJECT, StrictItem, fanout=True)
        async def on_item(self, message: StrictItem) -> None:
            self.received.append(message)

    outcome, received = await _dispatch(Consumer, shape)

    assert outcome == DispatchOutcome.OK, received
    assert received == [StrictItem(name="gadget", price=25.5)]


@pytest.mark.parametrize("shape", SHAPES.values(), ids=SHAPES.keys())
async def test_a_typed_listener_with_a_forbidding_model_receives_the_event(shape):
    class Consumer(CliffracerService):
        received: list = []

        @listener(SUBJECT, fanout=True)
        async def on_item(self, message: StrictItem) -> None:
            self.received.append(message)

    outcome, received = await _dispatch(Consumer, shape)

    assert outcome == DispatchOutcome.OK, received
    assert received == [StrictItem(name="gadget", price=25.5)]


@pytest.mark.parametrize("shape", SHAPES.values(), ids=SHAPES.keys())
async def test_CONTROL_a_typed_listener_whose_model_declares_the_id_receives_it(shape):
    """The removal is for models without the field; one that declares it keeps it."""

    class Consumer(CliffracerService):
        received: list = []

        @listener(SUBJECT, fanout=True)
        async def on_item(self, message: StrictItemWithId) -> None:
            self.received.append(message)

    outcome, received = await _dispatch(Consumer, shape)

    assert outcome == DispatchOutcome.OK, received
    assert [m.correlation_id for m in received] == ["corr-published"]
