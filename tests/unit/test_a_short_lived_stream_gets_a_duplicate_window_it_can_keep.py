"""A stream that keeps messages for under two minutes is declared with a window the server accepts.

The server refuses a stream whose duplicate window is longer than its age, and `StreamSpec` always
sent two minutes, so any `max_age_seconds` under 120 was refused at startup with a message that
named neither field. Declaring the window as `0` got past that, because the server then stores the
age as the window, and the next boot compared the declared two minutes with the stored age, found
drift, and refused with a message that listed the same subjects on both sides.

The broker here is the one measured on nats-server 2.10.29: a window longer than the age is
refused, and a window of `0` is stored as two minutes or the age, whichever is shorter.

A declaration that left the window out dumps without it. The refusal of a window longer than the
age reads whether the window was set, and a dump that wrote the default two minutes made the copy
read as one that set it, so validating the dump of a short-lived stream was refused. The template
and supervisor paths dump a config and validate the dump on every activation.
"""

from typing import Any

import pytest
from nats.js.api import StreamConfig
from pydantic import ValidationError

from cliffracer import ServiceConfig, StreamSpec
from cliffracer.core.jetstream import StreamDeclarationError, ensure_streams
from cliffracer.runners.templates import _copy_runtime_config

pytestmark = pytest.mark.unit


class _Info:
    def __init__(self, config: StreamConfig) -> None:
        self.config = config


class _Page:
    def __init__(self, configs: list[StreamConfig]) -> None:
        self._infos = [_Info(config) for config in configs]
        self.total = len(self._infos)

    def __iter__(self):
        return iter(self._infos)


class ServerRefusal(Exception):
    pass


class Broker:
    """A JetStream context that stores what it is given the way nats-server 2.10.29 does."""

    def __init__(self) -> None:
        self.streams: dict[str, StreamConfig] = {}
        self.updated: list[str] = []

    async def streams_info_iterator(self, offset: int = 0) -> _Page:
        return _Page(list(self.streams.values()))

    async def add_stream(self, config: StreamConfig) -> None:
        age = config.max_age
        window = config.duplicate_window
        if age and window and window > age:
            raise ServerRefusal("duplicates window can not be larger then max age")
        # A zero window is stored as two minutes, or the age when that is shorter.
        stored_window = window or (min(120.0, age) if age else 120.0)
        self.streams[config.name] = config.evolve(duplicate_window=stored_window)

    async def update_stream(self, config: StreamConfig) -> None:
        self.updated.append(config.name)
        self.streams[config.name] = config


def _spec(**fields: Any) -> StreamSpec:
    return StreamSpec(name="SHORT", subjects=["short.>"], **fields)


WINDOW_ASKED_FOR = [
    pytest.param({"max_age_seconds": 60}, 60.0, id="age-60-window-left-out"),
    pytest.param(
        {"max_age_seconds": 60, "duplicate_window_seconds": 0}, 60.0, id="age-60-window-0"
    ),
    pytest.param(
        {"max_age_seconds": 60, "duplicate_window_seconds": 30}, 30.0, id="age-60-window-30"
    ),
    pytest.param(
        {"max_age_seconds": 60, "duplicate_window_seconds": 60}, 60.0, id="age-60-window-60"
    ),
    pytest.param({"max_age_seconds": 0.5}, 0.5, id="age-half-a-second"),
    pytest.param({"max_age_seconds": 119}, 119.0, id="age-just-under-two-minutes"),
    # CONTROLS: a declaration that never hit the refusal asks for what it always asked for.
    pytest.param({}, 120.0, id="CONTROL-no-age"),
    pytest.param({"max_age_seconds": 120}, 120.0, id="CONTROL-age-two-minutes"),
    pytest.param({"max_age_seconds": 3600}, 120.0, id="CONTROL-age-an-hour"),
    pytest.param({"duplicate_window_seconds": 0}, 120.0, id="CONTROL-window-0-no-age"),
    pytest.param(
        {"max_age_seconds": 3600, "duplicate_window_seconds": 300}, 300.0, id="CONTROL-window-set"
    ),
    pytest.param({"duplicate_window_seconds": 120.0}, 120.0, id="CONTROL-window-120-no-age"),
]


@pytest.mark.parametrize(("fields", "window"), WINDOW_ASKED_FOR)
def test_the_window_a_declaration_asks_the_server_for(fields, window):
    assert _spec(**fields).to_stream_config().duplicate_window == window


@pytest.mark.parametrize(("fields", "window"), WINDOW_ASKED_FOR)
async def test_the_stream_is_created_and_a_second_boot_changes_nothing(fields, window):
    broker = Broker()
    spec = _spec(**fields)

    await ensure_streams(broker, [spec])
    assert broker.streams["SHORT"].duplicate_window == window
    await ensure_streams(broker, [spec])

    assert broker.updated == []


@pytest.mark.parametrize(("fields", "window"), WINDOW_ASKED_FOR)
def test_a_stream_the_server_stored_with_that_window_matches_the_declaration(fields, window):
    spec = _spec(**fields)
    stored = spec.to_stream_config().evolve(duplicate_window=window)

    assert spec.declared_differences(stored) == []


@pytest.mark.parametrize("reported", [None, 0, 60.0])
def test_every_shape_the_server_reports_the_default_window_in_matches(reported):
    spec = _spec(max_age_seconds=60)
    stored = spec.to_stream_config().evolve(duplicate_window=reported)

    assert spec.declared_differences(stored) == []


def test_the_validators_that_refuse_a_declaration_are_all_registered_on_the_model():
    """A validator whose decorator line is lost in a merge is an ordinary method that never runs."""
    registered = {
        decorator.cls_var_name
        for decorator in StreamSpec.__pydantic_decorators__.model_validators.values()
    }

    assert "_refuse_a_window_longer_than_the_age" in registered
    assert "_refuse_a_declaration_the_client_or_server_would" in registered


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"max_age_seconds": 60, "duplicate_window_seconds": 200}, id="longer"),
        pytest.param({"max_age_seconds": 60, "duplicate_window_seconds": 61}, id="a-second-longer"),
        pytest.param({"max_age_seconds": 60, "duplicate_window_seconds": 120.0}, id="two-minutes"),
        pytest.param({"max_age_seconds": 0.5, "duplicate_window_seconds": 1}, id="sub-second-age"),
    ],
)
def test_a_window_set_longer_than_the_age_cannot_be_built_and_names_both_fields(fields):
    with pytest.raises(ValidationError) as raised:
        _spec(**fields)

    text = str(raised.value)
    assert "duplicate_window_seconds" in text and "max_age_seconds" in text and "'SHORT'" in text


def _config(*specs: StreamSpec) -> ServiceConfig:
    return ServiceConfig(
        name="svc", health_port=0, jetstream_enabled=True, jetstream_streams=list(specs)
    )


def _what_a_copy_asks_for(copy: StreamSpec, original: StreamSpec) -> None:
    """Whether a window was set is the one thing a copy may not change, and what it asks for."""
    assert ("duplicate_window_seconds" in copy.model_fields_set) == (
        "duplicate_window_seconds" in original.model_fields_set
    )
    assert copy.to_stream_config() == original.to_stream_config()


UNSET_WINDOWS = [
    pytest.param({"max_age_seconds": 60}, id="age-60"),
    pytest.param({"max_age_seconds": 0.5}, id="age-half-a-second"),
    pytest.param({"max_age_seconds": 3600}, id="age-an-hour"),
    pytest.param({}, id="no-age"),
]


@pytest.mark.parametrize("fields", UNSET_WINDOWS)
def test_the_dump_of_a_declaration_that_left_the_window_out_validates_to_the_same_declaration(
    fields,
):
    spec = _spec(**fields)

    for dumped in (
        spec.model_dump(),
        spec.model_dump(mode="python", round_trip=True),
        spec.model_dump(mode="json"),
    ):
        assert "duplicate_window_seconds" not in dumped
        _what_a_copy_asks_for(StreamSpec.model_validate(dumped), spec)
    _what_a_copy_asks_for(StreamSpec.model_validate_json(spec.model_dump_json()), spec)


@pytest.mark.parametrize("fields", UNSET_WINDOWS)
def test_a_config_holding_such_a_stream_validates_its_own_dump(fields):
    config = _config(_spec(**fields))

    for copy in (
        ServiceConfig.model_validate(config.model_dump(mode="python", round_trip=True)),
        ServiceConfig.model_validate_json(config.model_dump_json()),
        _copy_runtime_config(config),
    ):
        _what_a_copy_asks_for(copy.jetstream_streams[0], config.jetstream_streams[0])


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"max_age_seconds": 60, "duplicate_window_seconds": 30}, id="short-age"),
        pytest.param({"max_age_seconds": 3600, "duplicate_window_seconds": 300}, id="long-age"),
        pytest.param({"max_age_seconds": 3600, "duplicate_window_seconds": 120.0}, id="default"),
        pytest.param({"duplicate_window_seconds": 0}, id="zero"),
    ],
)
def test_CONTROL_a_window_that_was_set_is_dumped_and_kept(fields):
    """The window is left out only when it was never set, not whenever it equals the default."""
    spec = _spec(**fields)

    dumped = spec.model_dump()
    assert dumped["duplicate_window_seconds"] == fields["duplicate_window_seconds"]
    _what_a_copy_asks_for(StreamSpec.model_validate(dumped), spec)
    _what_a_copy_asks_for(
        _copy_runtime_config(_config(spec)).jetstream_streams[0],
        spec,
    )


def test_CONTROL_a_dump_that_names_a_window_longer_than_the_age_is_still_refused():
    """The refusal still reads the dump: a copy that sets the window is held to the age."""
    dumped = _spec(max_age_seconds=60).model_dump()
    dumped["duplicate_window_seconds"] = 120.0

    with pytest.raises(ValidationError, match="longer than max_age_seconds"):
        StreamSpec.model_validate(dumped)


async def test_CONTROL_the_broker_double_refuses_a_window_longer_than_the_age():
    """The refusal above is the server's own rule, which the double keeps, so the check is real."""
    unchecked = StreamSpec.model_construct(
        name="SHORT",
        subjects=["short.>"],
        storage="file",
        retention="limits",
        max_age_seconds=60,
        duplicate_window_seconds=200,
    )

    with pytest.raises(ServerRefusal, match="larger then max age"):
        await ensure_streams(Broker(), [unchecked])


async def test_an_update_asks_for_the_window_a_creation_would():
    broker = Broker()
    await ensure_streams(broker, [_spec(max_age_seconds=3600)])
    assert broker.streams["SHORT"].duplicate_window == 120.0

    await ensure_streams(broker, [_spec(max_age_seconds=60)], allow_update=True)

    assert broker.updated == ["SHORT"]
    assert broker.streams["SHORT"].duplicate_window == 60.0
    assert broker.streams["SHORT"].max_age == 60


def test_the_description_of_a_service_reports_the_window_it_asks_the_server_for():
    from cliffracer import CliffracerService, ServiceConfig
    from cliffracer.introspect import describe

    class Svc(CliffracerService):
        pass

    config = ServiceConfig(
        name="svc",
        health_port=0,
        jetstream_enabled=True,
        jetstream_streams=[
            _spec(max_age_seconds=60),
            StreamSpec(name="LONG", subjects=["long.>"], max_age_seconds=3600),
        ],
    )

    windows = {s.name: s.duplicate_window_seconds for s in describe(Svc, config=config).streams}

    assert windows == {"SHORT": 60.0, "LONG": 120.0}


async def test_a_different_subject_list_names_the_subjects_on_both_sides():
    broker = Broker()
    await ensure_streams(broker, [StreamSpec(name="DRIFT", subjects=["drift.a"])])

    with pytest.raises(StreamDeclarationError) as raised:
        await ensure_streams(broker, [StreamSpec(name="DRIFT", subjects=["drift.b"])])

    text = str(raised.value)
    assert "subjects: declared ['drift.b'], on the broker ['drift.a']" in text
    assert "duplicate_window_seconds" not in text and "max_age_seconds" not in text


async def test_a_different_window_names_the_window_and_not_the_subjects():
    broker = Broker()
    await ensure_streams(broker, [_spec(max_age_seconds=3600, duplicate_window_seconds=300)])

    with pytest.raises(StreamDeclarationError) as raised:
        await ensure_streams(broker, [_spec(max_age_seconds=3600, duplicate_window_seconds=200)])

    text = str(raised.value)
    assert "duplicate_window_seconds: declared 200.0, on the broker 300.0" in text
    assert (
        "subjects"
        not in text.split("differs from this service's declaration in")[1].split(". Refusing")[0]
    )


async def test_every_differing_field_is_named():
    broker = Broker()
    await ensure_streams(broker, [_spec(max_age_seconds=3600)])

    with pytest.raises(StreamDeclarationError) as raised:
        await ensure_streams(broker, [_spec(max_age_seconds=7200, storage="memory")])

    text = str(raised.value)
    assert "storage: declared 'memory', on the broker 'file'" in text
    assert "max_age_seconds: declared 7200" in text
