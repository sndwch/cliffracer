"""A model published with `publish_event` or `broadcast_message` reaches its listener as the model.

The publish side wrote a model by alias, as `to_jsonable_python` does, so a listener whose model reads
by field name only (`validate_by_alias=False`, `validate_by_name=True`, an aliased field) refused it
and the event was dead-lettered. The publish side now chooses each model's form as the RPC call path
does (`wire_models`): the form its class reads back as the argument, alias first, written a level at a
time for a tree whose levels need different forms.

Every shape goes to a listener that takes the model among other parameters and to one that takes it
alone, on core NATS, a JetStream push consumer and a JetStream pull consumer, from a publisher writing
JSON and one writing msgpack. What each listener received is compared with what was published, by the
declared type's dump.
"""

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from cliffracer import CliffracerService, ServiceConfig, broadcast, listener
from cliffracer.core.service_config import StreamSpec

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


class ByName(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    x: int = Field(alias="xx")


class AliasOnly(BaseModel):
    x: int = Field(alias="xx")


class Populatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    x: int = Field(alias="xx")


class Plain(BaseModel):
    x: int


class Inner(BaseModel):
    y: int = Field(alias="yy")


class ByNameOverAliasInner(BaseModel):
    """No whole-value form is read: the outer reads names, the inner an alias."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    x: int = Field(alias="xx")
    inner: Inner


SHAPES: dict[str, tuple[type[BaseModel], BaseModel]] = {
    "by_name": (ByName, ByName(x=1)),
    "alias_only": (AliasOnly, AliasOnly(xx=2)),
    "populatable": (Populatable, Populatable(xx=3)),
    "plain": (Plain, Plain(x=4)),
    "nested": (ByNameOverAliasInner, ByNameOverAliasInner(x=5, inner=Inner(yy=6))),
}
KINDS = ("multi", "single")


def _dump(annotation: Any, value: Any) -> str:
    return json.dumps(TypeAdapter(annotation).dump_python(value, mode="json"), sort_keys=True)


def _outcome(model: type[BaseModel], published: BaseModel, received: Any) -> str:
    """ "equal" when the listener was given the declared model holding the published values."""
    if isinstance(received, model) and _dump(model, received) == _dump(model, published):
        return "equal"
    return f"different: {received!r}"


def _listener(
    subject: str, model: type[BaseModel], kind: str, transport: str, seen: dict, key: str
):
    """A listener method on `subject` recording, under `key`, what it was given for `item`."""
    if transport == "core":
        decorate = listener(subject, fanout=True)
    else:
        durable = key.replace(".", "-").replace(":", "-")
        decorate = listener(subject, durable=durable, pull=(transport == "pull"))

    if kind == "multi":

        async def on(self, subject: str, item, n: int = 0):
            seen[key] = item

        on.__annotations__ = {"subject": str, "item": model, "n": int, "return": None}
    else:

        async def on(self, subject: str, item):
            seen[key] = item

        on.__annotations__ = {"subject": str, "item": model, "return": None}
    on.__name__ = f"on_{key.replace('.', '_').replace(':', '_')}"
    return decorate(on)


def _consumer(transport: str, seen: dict, tag: str) -> CliffracerService:
    namespace: dict[str, Any] = {}
    for shape, (model, _) in SHAPES.items():
        for kind in KINDS:
            key = f"{kind}.{shape}"
            subject = f"evform{tag}.{transport}.{kind}.{shape}"
            handler = _listener(subject, model, kind, transport, seen, f"{tag}.{key}")
            namespace[handler.__name__] = handler
    cls = type("Consumer", (CliffracerService,), namespace)
    name = f"evform_consumer_{tag}_{transport}"
    config: dict[str, Any] = {"name": name, "health_port": 0}
    if transport != "core":
        stream = f"EVFORM_{tag.upper()}_{transport.upper()}"
        config |= {
            "jetstream_enabled": True,
            "jetstream_pull_timeout": 0.3,
            "jetstream_streams": [
                StreamSpec(name=stream, subjects=[f"evform{tag}.{transport}.>"]),
                StreamSpec(name=f"{stream}_DLQ", subjects=[f"dlq.{name}"]),
            ],
        }
    return cls(ServiceConfig(**config))


class _Seen(dict):
    """What the listeners were given, by key, and an event set once `expected` keys are in."""

    def __init__(self, expected: int) -> None:
        super().__init__()
        self.expected = expected
        self.full = asyncio.Event()

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        if len(self) >= self.expected:
            self.full.set()


async def _wait_for(seen: "_Seen", keys: list[str]) -> None:
    """Wait for every listener on the event the last one sets, with a ceiling generous enough for a
    loaded host; a listener that never ran shows as "lost" in the outcome after it."""
    try:
        await asyncio.wait_for(seen.full.wait(), timeout=20.0)
    except TimeoutError:
        pass


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
@pytest.mark.parametrize("transport", ["core", "push", "pull"])
async def test_publish_event_delivers_each_model_as_its_listener_declares_it(transport, fmt):
    tag = f"p{fmt[0]}"
    seen = _Seen(expected=len(SHAPES) * len(KINDS))
    consumer = _consumer(transport, seen, tag)
    publisher = CliffracerService(
        ServiceConfig(
            name=f"evform_publisher_{tag}_{transport}",
            health_port=0,
            serialization_format=fmt,
        )
    )
    await consumer.start()
    await publisher.start()
    try:
        for shape, (_, value) in SHAPES.items():
            for kind in KINDS:
                await publisher.publish_event(f"evform{tag}.{transport}.{kind}.{shape}", item=value)
        keys = [f"{tag}.{kind}.{shape}" for shape in SHAPES for kind in KINDS]
        await _wait_for(seen, keys)
    finally:
        await publisher.stop()
        await consumer.stop()

    outcomes = {
        f"{kind}.{shape}": (
            "lost"
            if f"{tag}.{kind}.{shape}" not in seen
            else _outcome(model, value, seen[f"{tag}.{kind}.{shape}"])
        )
        for shape, (model, value) in SHAPES.items()
        for kind in KINDS
    }
    assert outcomes == dict.fromkeys(outcomes, "equal"), outcomes


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
async def test_broadcast_message_delivers_each_model_as_its_handler_declares_it(fmt):
    seen = _Seen(expected=len(SHAPES))
    namespace: dict[str, Any] = {}
    for shape, (model, _) in SHAPES.items():

        def make(key: str, model: type[BaseModel]):
            async def on(self, subject: str, item, n: int = 0):
                seen[key] = item

            on.__annotations__ = {"subject": str, "item": model, "n": int, "return": None}
            on.__name__ = f"on_{key}"
            return broadcast(f"evformb{fmt[0]}.{key}")(on)

        handler = make(shape, model)
        namespace[handler.__name__] = handler
    consumer = type("Broadcasts", (CliffracerService,), namespace)(
        ServiceConfig(name=f"evform_broadcasts_{fmt}", health_port=0)
    )
    publisher = CliffracerService(
        ServiceConfig(name=f"evform_broadcaster_{fmt}", health_port=0, serialization_format=fmt)
    )
    await consumer.start()
    await publisher.start()
    try:
        for shape, (_, value) in SHAPES.items():
            await publisher.broadcast_message(f"evformb{fmt[0]}.{shape}", item=value)
        await _wait_for(seen, list(SHAPES))
    finally:
        await publisher.stop()
        await consumer.stop()

    outcomes = {
        shape: ("lost" if shape not in seen else _outcome(model, value, seen[shape]))
        for shape, (model, value) in SHAPES.items()
    }
    assert outcomes == dict.fromkeys(outcomes, "equal"), outcomes
