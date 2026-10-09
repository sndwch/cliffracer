"""`describe` takes the arguments it reads and the markers discovery reads, and nothing else.

A misspelt keyword was swallowed by a `**kwargs` and the default used in its place, so a typo in
`version=` produced a plausible wrong version rather than an error. The service and version
fallbacks read `service_name` and `version` off the class with a plain `getattr`, so a property of
either name made the description unserialisable. A stream's published fields included one nothing
produces and left out the duplicate window a stream really has, and a validated listener read a
`pull` marker that `validated_listener` never writes.
"""

import json

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.jetstream import StreamSpec
from cliffracer.introspect import Description, StreamDescription, canonical, describe

pytestmark = pytest.mark.unit


class Evt(BaseModel):
    n: int


class Plain(CliffracerService):
    @listener("evt.a", fanout=True)
    async def on_a(self, n: int) -> None: ...


@pytest.mark.parametrize("typo", ["versoin", "servcie", "confg", "name", "namespace"])
def test_a_keyword_describe_does_not_take_is_an_error(typo):
    with pytest.raises(TypeError, match=typo):
        describe(Plain, **{typo: "2"})


def test_CONTROL_the_keywords_it_does_take_are_read():
    config = ServiceConfig(name="from_config", health_port=0, version="9.9.9")

    explicit = describe(Plain, service="s", version="2", config=config)
    from_config = describe(Plain, config=config)
    bare = describe(Plain)

    assert (explicit.service, explicit.version) == ("s", "2")
    assert (from_config.service, from_config.version) == ("from_config", "9.9.9")
    assert (bare.service, bare.version) == ("plain", ServiceConfig.model_fields["version"].default)


def test_a_property_named_service_name_or_version_is_not_read_as_the_name():
    class WithProperties(CliffracerService):
        @property
        def service_name(self) -> str:
            raise AssertionError("a property is never evaluated")

        @property
        def version(self) -> str:
            raise AssertionError("a property is never evaluated")

    described = describe(WithProperties)

    assert described.service == "withproperties"
    assert described.version == ServiceConfig.model_fields["version"].default
    assert json.loads(canonical(described.to_dict()))["service"] == "withproperties"


def test_a_stream_is_described_with_its_duplicate_window():
    config = ServiceConfig(
        name="s",
        health_port=0,
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="EVT", subjects=["evt.>"], duplicate_window_seconds=30.0),
            StreamSpec(name="OTHER", subjects=["other.>"]),
        ],
    )

    described = describe(Plain, config=config)

    assert {s.name: s.duplicate_window_seconds for s in described.streams} == {
        "EVT": 30.0,
        "OTHER": 120.0,
    }
    assert described.to_dict()["streams"][0]["duplicate_window_seconds"] == 30.0


def test_a_description_from_before_the_window_was_published_reads_the_default():
    wire = StreamDescription(name="EVT", subjects=["evt.>"]).to_dict()
    del wire["duplicate_window_seconds"]

    assert StreamDescription.from_dict(wire).duplicate_window_seconds == 120.0


def test_a_stream_description_carries_no_field_nothing_produces():
    assert "max_consumers" not in StreamDescription(name="EVT").to_dict()


def test_a_validated_listener_is_never_described_as_a_pull_consumer():
    """Discovery does not read a `pull` marker off a validated listener, so neither does
    describe, even if one is somehow on the function."""

    class Svc(CliffracerService):
        @validated_listener("evt.a", Evt, durable="d")
        async def on_a(self, event: Evt) -> None: ...

    Svc.on_a._cliffracer_event_pull = {"evt.a"}  # type: ignore[attr-defined]
    config = ServiceConfig(name="s", health_port=0, jetstream_enabled=True)

    (described,) = describe(Svc, config=config).listeners
    service = Svc(config)
    service._discover_handlers()
    registry = service.container.registry

    assert described.pull is False
    assert "evt.a" not in registry.event_pull


def test_a_listener_found_by_pattern_and_its_derived_flags():
    class Svc(CliffracerService):
        @validated_listener("evt.model", Evt, fanout=True)
        async def on_model(self, event: Evt) -> None: ...

        @listener("evt.plain", fanout=True)
        async def on_plain(self, n: int) -> None: ...

    described = describe(Svc)

    model, plain = described.listener("evt.model"), described.listener("evt.plain")
    assert model is not None and plain is not None
    assert (model.is_validated, model.model_schema_hash is not None) == (True, True)
    assert (plain.is_validated, plain.model_schema_hash) == (False, None)
    assert (model.is_broadcast, plain.is_broadcast) == (True, True)
    assert (model.subject, model.handler) == ("evt.model", "on_model")
    assert described.listener("evt.missing") is None
    assert Description.from_dict(described.to_dict()) == described
