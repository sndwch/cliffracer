"""A validated listener whose schema declares `correlation_id` receives the published value.

An event may carry `correlation_id` among its fields, flat or inside an
envelope's `data`. Dispatch removes it before building a listener's model, so
that a schema without the field, including one that forbids extra fields, still
validates. A schema that declares the field is asking for it. A typed
`@listener` taking such a model received the value, but a `@validated_listener`
with the same schema received its default, because its branch removed the key
whether or not the schema declared it.
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


class Declares(BaseModel):
    name: str
    correlation_id: str | None = None


class DeclaresAndForbidsExtras(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    correlation_id: str | None = None


class ForbidsExtras(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


FIELDS = {"name": "gadget", "correlation_id": "corr-published"}

SHAPES = pytest.mark.parametrize(
    "body",
    [
        FIELDS,
        {
            "data": FIELDS,
            "timestamp": datetime(2026, 1, 2, 3, 4, 5).isoformat(),
            "source_service": "producer",
            "correlation_id": "corr-published",
        },
    ],
    ids=["flat", "envelope"],
)


async def _deliver(schema, body: dict, *, validated: bool):
    decorate = (
        validated_listener(SUBJECT, schema, fanout=True)
        if validated
        else listener(SUBJECT, fanout=True)
    )

    class Consumer(CliffracerService):
        received: list = []

        @decorate
        async def on_item(self, message: schema) -> None:  # type: ignore[valid-type]
            self.received.append(message)

    svc = Consumer(ServiceConfig(name="consumer"))
    svc._discover_handlers()
    message = MockMessage(
        subject=SUBJECT,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    outcome = await svc.container._dispatch_event(message, pattern=SUBJECT)
    return outcome, svc.received


@SHAPES
@pytest.mark.parametrize(
    "schema", [Declares, DeclaresAndForbidsExtras], ids=["declares", "declares-and-forbids"]
)
async def test_a_validated_schema_that_declares_the_field_receives_the_published_value(
    body, schema
):
    outcome, received = await _deliver(schema, body, validated=True)

    assert outcome == DispatchOutcome.OK, received
    assert [m.correlation_id for m in received] == ["corr-published"]


@SHAPES
async def test_the_validated_and_typed_listeners_receive_the_same_model(body):
    _, validated = await _deliver(DeclaresAndForbidsExtras, body, validated=True)
    _, typed = await _deliver(DeclaresAndForbidsExtras, body, validated=False)

    assert (
        validated
        == typed
        == [DeclaresAndForbidsExtras(name="gadget", correlation_id="corr-published")]
    )


@SHAPES
async def test_CONTROL_a_validated_schema_without_the_field_still_receives_the_event(body):
    """The removal stays for a schema that does not declare the field: one that
    forbids extras would otherwise refuse every event carrying an id."""
    outcome, received = await _deliver(ForbidsExtras, body, validated=True)

    assert outcome == DispatchOutcome.OK, received
    assert received == [ForbidsExtras(name="gadget")]
