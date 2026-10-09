"""Payload validation against a handler's declared types is one step, shared.

It existed four times: once in `ValidationExtension` for an RPC, and three times inline in the event
dispatcher (a `@validated_listener`'s schema, a typed listener's one model, and a typed listener's
parameters). Each removed the message's `correlation_id` by its own rule, and the copies had
drifted: the extension removed it only from a `dict`, the dispatcher from a `dict` too but unless
the model declared it. `validate_payload` is the one step; the failure handling stays with each path,
since an RPC refuses and replies and an event follows its `on_invalid` policy.

The helper's rule is read directly. That every path uses it is read by spying on it at the four
places that call it, since a copy put back inline would behave the same until the next rule moves.
"""

import json
from collections import UserDict
from types import MappingProxyType

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from cliffracer import CliffracerService, ServiceConfig, listener, rpc, validated_listener
from cliffracer.core.container import DispatchOutcome
from cliffracer.core.extension import WorkerContext
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

SUBJECT = "items.created"


class Plain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class Declares(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    correlation_id: str | None = None


def _validate_payload():
    from cliffracer.core.validation import validate_payload

    return validate_payload


@pytest.mark.parametrize(
    "make", [dict, MappingProxyType, UserDict], ids=["dict", "mappingproxy", "userdict"]
)
def test_an_objects_correlation_id_is_removed_when_the_model_does_not_declare_it(make):
    model = _validate_payload()(Plain, make({"name": "a", "correlation_id": "c-1"}))

    assert model == Plain(name="a")


@pytest.mark.parametrize(
    "make", [dict, MappingProxyType, UserDict], ids=["dict", "mappingproxy", "userdict"]
)
def test_an_objects_correlation_id_is_kept_when_the_model_declares_it(make):
    model = _validate_payload()(Declares, make({"name": "a", "correlation_id": "c-1"}))

    assert model == Declares(name="a", correlation_id="c-1")


def test_the_object_given_is_not_changed_by_reading_it():
    payload = {"name": "a", "correlation_id": "c-1"}

    _validate_payload()(Plain, payload)

    assert payload == {"name": "a", "correlation_id": "c-1"}


@pytest.mark.parametrize("payload", [42, "text", [1], None], ids=repr)
def test_a_value_that_is_not_an_object_is_validated_as_it_is_and_refused(payload):
    with pytest.raises(ValidationError):
        _validate_payload()(Plain, payload)


def test_a_failure_is_pydantics_error_for_the_caller_to_route():
    with pytest.raises(ValidationError) as caught:
        _validate_payload()(Plain, {"name": 5})

    assert caught.value.errors()[0]["loc"] == ("name",)


class _Spy:
    """Counts the calls a module makes to `validate_payload`, by the name it imported it under."""

    def __init__(self, monkeypatch, module: str) -> None:
        self.calls = 0
        import importlib

        target = importlib.import_module(module)
        real = target.validate_payload

        def spy(model_type, payload):
            self.calls += 1
            return real(model_type, payload)

        monkeypatch.setattr(target, "validate_payload", spy)


@pytest.fixture
def spies(monkeypatch):
    return {
        "rpc": _Spy(monkeypatch, "cliffracer.core.validation_extension"),
        "event": _Spy(monkeypatch, "cliffracer.core.dispatch.events"),
    }


async def test_an_rpc_is_validated_by_the_shared_step(spies):
    class Svc(CliffracerService):
        @rpc
        async def echo(self, x: int) -> dict[str, int]:
            return {"x": x}

    svc = Svc(ServiceConfig(name="s", health_port=0))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    extension = next(
        e for e in svc.container.extensions if type(e).__name__ == "ValidationExtension"
    )
    ctx = WorkerContext(
        kind="rpc",
        subject="s.rpc.echo",
        headers={},
        correlation_id=None,
        payload={"x": 1},
        raw=None,
    )
    ctx.data["handler_name"] = "echo"

    await extension.worker_setup(ctx)

    assert (spies["rpc"].calls, spies["event"].calls) == (1, 0)


async def _deliver(decorator, handler_signature_model, body):
    class Consumer(CliffracerService):
        received: list = []

        if handler_signature_model is None:

            @decorator
            async def on_item(self, name: str, count: int = 0) -> None:
                self.received.append((name, count))

        else:

            @decorator
            async def on_item(self, message: handler_signature_model) -> None:  # type: ignore[valid-type]
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


EVENT_PATHS = [
    pytest.param(validated_listener(SUBJECT, Plain, fanout=True), Plain, id="validated_listener"),
    pytest.param(listener(SUBJECT, fanout=True), Plain, id="typed-one-model"),
    pytest.param(listener(SUBJECT, fanout=True), None, id="typed-parameters"),
]


@pytest.mark.parametrize(("decorator", "model"), EVENT_PATHS)
async def test_each_event_path_is_validated_by_the_shared_step(spies, decorator, model):
    body = {"name": "gadget", "correlation_id": "corr-1"}

    outcome, received = await _deliver(decorator, model, body)

    assert outcome == DispatchOutcome.OK and len(received) == 1, received
    assert (spies["event"].calls, spies["rpc"].calls) == (1, 0)


@pytest.mark.parametrize(("decorator", "model"), EVENT_PATHS)
async def test_each_event_path_removes_the_id_the_way_the_rpc_does(decorator, model):
    """One rule for what is removed: a schema that forbids extras still receives an event with an id."""
    outcome, received = await _deliver(
        decorator, model, {"name": "gadget", "correlation_id": "c-1"}
    )

    assert outcome == DispatchOutcome.OK, received


async def test_a_typed_listener_reads_a_mapping_payload_as_an_object_not_as_one_value(monkeypatch):
    """A handler with one parameter is given a payload that is not an object as that parameter's
    value. A `Mapping` is an object, as it is for an RPC, so it must not be wrapped as the value."""
    monkeypatch.setattr(
        "cliffracer.core.dispatch.events.deserialize_payload",
        lambda *args, **kwargs: MappingProxyType({"name": "gadget"}),
    )

    class Consumer(CliffracerService):
        received: list = []

        @listener(SUBJECT, fanout=True)
        async def on_item(self, name: str) -> None:
            self.received.append(name)

    svc = Consumer(ServiceConfig(name="consumer"))
    svc._discover_handlers()
    message = MockMessage(subject=SUBJECT, data=b"{}", headers={"Content-Type": "application/json"})

    outcome = await svc.container._dispatch_event(message, pattern=SUBJECT)

    assert outcome == DispatchOutcome.OK, outcome
    assert svc.received == ["gadget"]


async def test_CONTROL_a_value_that_is_not_an_object_is_still_the_one_parameter_of_a_typed_listener():
    class Consumer(CliffracerService):
        received: list = []

        @listener(SUBJECT, fanout=True)
        async def on_item(self, name: str) -> None:
            self.received.append(name)

    svc = Consumer(ServiceConfig(name="consumer"))
    svc._discover_handlers()
    message = MockMessage(
        subject=SUBJECT, data=b'"gadget"', headers={"Content-Type": "application/json"}
    )

    outcome = await svc.container._dispatch_event(message, pattern=SUBJECT)

    assert outcome == DispatchOutcome.OK and svc.received == ["gadget"]
