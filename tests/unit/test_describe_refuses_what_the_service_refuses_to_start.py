"""`describe` refuses a class the service would refuse to start, for the rules it can apply.

Its docstring promised that an unannotated handler raises here "for the same reason the service
refuses to start", and held only for `@rpc`: the three event branches never called the event
spec builders, so a listener taking `**data` described fine while discovery refused it, and the
generator emitted a client for a service that could not boot. The same went for two listeners on
one subject and for the pull rules. These drive one class through both walks and require them to
agree, and require the rules that need a configuration to apply only when one is given.
"""

import sys

import pytest
from pydantic import BaseModel

from cliffracer import (
    CliffracerService,
    ConfigurationError,
    ServiceConfig,
    broadcast,
    listener,
    rpc,
    timer,
    validated_listener,
)
from cliffracer.core.jetstream import StreamSpec
from cliffracer.core.typed_rpc import UntypedHandler
from cliffracer.generate_client.cli import main
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit


class Evt(BaseModel):
    n: int


def _untyped_listener():
    class S(CliffracerService):
        @listener("evt.a", fanout=True)
        async def on_a(self, subject: str, **data) -> None: ...

    return S, UntypedHandler


def _unannotated_listener_subject():
    class S(CliffracerService):
        @listener("evt.a", fanout=True)
        async def on_a(self, subject, n: int) -> None: ...

    return S, UntypedHandler


def _untyped_broadcast():
    class S(CliffracerService):
        @broadcast("evt.a")
        async def on_a(self, subject: str, **data) -> None: ...

    return S, UntypedHandler


def _validated_with_an_extra_payload_parameter():
    class S(CliffracerService):
        @validated_listener("evt.a", Evt, fanout=True)
        async def on_a(self, event: Evt, extra: int) -> None: ...

    return S, UntypedHandler


def _two_listeners_on_one_subject():
    class S(CliffracerService):
        @listener("evt.a", fanout=True)
        async def first(self, n: int) -> None: ...

        @listener("evt.a", fanout=True)
        async def second(self, n: int) -> None: ...

    return S, ConfigurationError


def _a_pull_listener_with_no_durable():
    class S(CliffracerService):
        @listener("evt.a", pull=True)
        async def on_a(self, n: int) -> None: ...

    return S, ConfigurationError


def _a_pull_listener_with_fanout():
    class S(CliffracerService):
        @listener("evt.a", durable="d", pull=True, fanout=True)
        async def on_a(self, n: int) -> None: ...

    return S, ConfigurationError


def _a_listener_with_neither_a_durable_nor_fanout():
    class S(CliffracerService):
        @listener("evt.a")
        async def on_a(self, n: int) -> None: ...

    return S, ConfigurationError


def _a_validated_listener_with_neither_a_durable_nor_fanout():
    class S(CliffracerService):
        @validated_listener("evt.a", Evt)
        async def on_a(self, event: Evt) -> None: ...

    return S, ConfigurationError


def _two_subjects_sharing_a_durable():
    class S(CliffracerService):
        @listener("evt.a", durable="shared")
        async def first(self, n: int) -> None: ...

        @listener("evt.b", durable="shared")
        async def second(self, n: int) -> None: ...

    return S, ConfigurationError


def _a_private_listener():
    class S(CliffracerService):
        @listener("evt.a", fanout=True)
        async def _on_a(self, n: int) -> None: ...

    return S, ConfigurationError


def _a_private_rpc():
    class S(CliffracerService):
        @rpc
        async def _hidden(self, n: int) -> int: ...

    return S, ConfigurationError


def _a_private_timer():
    class S(CliffracerService):
        @timer(interval=60)
        async def _tick(self) -> None: ...

    return S, ConfigurationError


def _a_listener_named_for_a_framework_method():
    class S(CliffracerService):
        @listener("evt.a", fanout=True)
        async def health_check(self, n: int) -> None: ...

    return S, ConfigurationError


REFUSED_WITHOUT_A_CONFIG = [
    _untyped_listener,
    _unannotated_listener_subject,
    _untyped_broadcast,
    _validated_with_an_extra_payload_parameter,
    _two_listeners_on_one_subject,
    _a_pull_listener_with_no_durable,
    _a_pull_listener_with_fanout,
    _a_listener_with_neither_a_durable_nor_fanout,
    _a_validated_listener_with_neither_a_durable_nor_fanout,
    _two_subjects_sharing_a_durable,
    _a_private_listener,
    _a_private_rpc,
    _a_private_timer,
    _a_listener_named_for_a_framework_method,
]


def _discovery_refusal(cls: type, config: ServiceConfig) -> type[BaseException]:
    service = cls(config)
    with pytest.raises((UntypedHandler, ConfigurationError)) as caught:
        service._discover_handlers()
    return type(caught.value)


@pytest.mark.parametrize("build", REFUSED_WITHOUT_A_CONFIG)
def test_describe_refuses_what_discovery_refuses_with_the_same_error(build):
    cls, expected = build()

    with pytest.raises(expected):
        describe(cls, service="s", version="1")

    config = ServiceConfig(name="s", health_port=0, jetstream_enabled=True)
    assert _discovery_refusal(cls, config) is expected


def test_the_untyped_listener_names_the_handler_and_the_rule():
    cls, _ = _untyped_listener()

    with pytest.raises(UntypedHandler, match=r"S\.on_a.*\*data is not allowed"):
        describe(cls, service="s", version="1")


def test_CONTROL_a_listener_the_service_would_start_is_described():
    class S(CliffracerService):
        @listener("evt.a", fanout=True)
        async def on_a(self, subject: str, n: int) -> None: ...

        @validated_listener("evt.b", Evt, durable="b")
        async def on_b(self, event: Evt) -> None: ...

        @broadcast("evt.c")
        async def on_c(self, subject: str, n: int) -> None: ...

    description = describe(S, service="s", version="1")

    assert sorted(item.pattern for item in description.listeners) == ["evt.a", "evt.b", "evt.c"]


def test_CONTROL_the_same_subject_as_a_plain_and_a_cross_namespace_listener_is_two_subjects():
    class S(CliffracerService):
        @listener("evt.a", fanout=True)
        async def plain(self, n: int) -> None: ...

        @listener("evt.a", fanout=True, cross_namespace=True)
        async def across(self, n: int) -> None: ...

    assert len(describe(S, service="s", version="1").listeners) == 2


def test_CONTROL_a_pull_listener_with_a_durable_is_described_with_no_config():
    class S(CliffracerService):
        @listener("evt.a", durable="d", pull=True)
        async def on_a(self, n: int) -> None: ...

    assert describe(S, service="s", version="1").listeners[0].pull is True


def test_a_pull_listener_is_refused_when_the_given_config_has_no_jetstream():
    class S(CliffracerService):
        @listener("evt.a", durable="d", pull=True)
        async def on_a(self, n: int) -> None: ...

    config = ServiceConfig(name="s", health_port=0, jetstream_enabled=False)

    with pytest.raises(ConfigurationError, match="jetstream_enabled=False"):
        describe(S, config=config)


def _durable_with_fanout():
    class S(CliffracerService):
        @listener("evt.a", durable="d", fanout=True)
        async def on_a(self, n: int) -> None: ...

    return S


def test_a_durable_with_fanout_is_refused_when_the_given_config_has_jetstream():
    config = ServiceConfig(name="s", health_port=0, jetstream_enabled=True)

    with pytest.raises(ConfigurationError, match="BOTH a durable and fanout"):
        describe(_durable_with_fanout(), config=config)


def test_a_durable_with_fanout_is_described_with_no_config_and_with_jetstream_off():
    cls = _durable_with_fanout()

    assert describe(cls, service="s", version="1").listeners[0].fanout is True
    off = ServiceConfig(name="s", health_port=0, jetstream_enabled=False)
    assert describe(cls, config=off).listeners[0].fanout is True
    assert _discovery_accepts(cls, off)


def _discovery_accepts(cls: type, config: ServiceConfig) -> bool:
    cls(config)._discover_handlers()
    return True


def _cross_namespace_listener():
    class S(CliffracerService):
        @listener("evt.a", fanout=True, cross_namespace=True)
        async def on_a(self, n: int) -> None: ...

    return S


def _cross_namespace_validated_listener():
    class S(CliffracerService):
        @validated_listener("evt.a", Evt, fanout=True, cross_namespace=True)
        async def on_a(self, event: Evt) -> None: ...

    return S


CROSS_NAMESPACE_LISTENERS = [_cross_namespace_listener, _cross_namespace_validated_listener]


@pytest.mark.parametrize("build", CROSS_NAMESPACE_LISTENERS)
def test_a_cross_namespace_listener_with_no_namespace_is_refused_by_describe_as_by_discovery(build):
    cls = build()
    config = ServiceConfig(name="s", health_port=0)

    with pytest.raises(ConfigurationError, match="no namespace to span") as described:
        describe(cls, config=config)

    assert _discovery_refusal(cls, config) is ConfigurationError
    with pytest.raises(ConfigurationError) as discovered:
        cls(config)._discover_handlers()
    assert str(described.value) == str(discovered.value)
    assert "S.on_a" in str(described.value)


@pytest.mark.parametrize("build", CROSS_NAMESPACE_LISTENERS)
def test_CONTROL_a_cross_namespace_listener_is_described_with_a_namespace_or_with_no_config(build):
    cls = build()
    config = ServiceConfig(name="s", health_port=0, namespace="utils")

    (listening,) = describe(cls, config=config).listeners
    assert listening.cross_namespace is True
    assert listening.effective_subject == "*.evt.a"
    assert _discovery_accepts(cls, config)

    assert describe(cls, service="s", version="1").listeners[0].cross_namespace is True


def test_CONTROL_a_listener_that_is_not_cross_namespace_is_described_with_no_namespace():
    class S(CliffracerService):
        @listener("evt.a", fanout=True)
        async def plain(self, n: int) -> None: ...

    config = ServiceConfig(name="s", health_port=0)

    assert describe(S, config=config).listeners[0].effective_subject == "evt.a"
    assert _discovery_accepts(S, config)


def _two_durable_listeners():
    class S(CliffracerService):
        @listener("evt.a", durable="d")
        async def on_a(self, n: int) -> None: ...

        @listener("evt.b", durable="e")
        async def on_b(self, n: int) -> None: ...

    return S


def _jetstream_config(enabled: bool, **overrides) -> ServiceConfig:
    return ServiceConfig(
        name="s",
        health_port=0,
        jetstream_enabled=enabled,
        jetstream_streams=[StreamSpec(name="EVT", subjects=["evt.>"])],
        **overrides,
    )


def test_a_durable_listener_is_refused_by_describe_as_by_discovery_when_jetstream_is_off():
    cls = _two_durable_listeners()
    config = _jetstream_config(False)

    with pytest.raises(ConfigurationError, match="inert while jetstream_enabled is False") as seen:
        describe(cls, config=config)

    with pytest.raises(ConfigurationError) as discovered:
        cls(config)._discover_handlers()
    assert str(seen.value) == str(discovered.value)


def test_CONTROL_the_same_durable_listeners_are_described_when_jetstream_is_on():
    cls = _two_durable_listeners()
    config = _jetstream_config(True)

    described = describe(cls, config=config)

    assert [(item.durable, item.queue_group) for item in described.listeners] == [
        ("d", "d"),
        ("e", "e"),
    ]
    assert _discovery_accepts(cls, config)


def _a_fanout_listener_with_a_durable_it_does_not_use():
    class S(CliffracerService):
        @listener("evt.a", durable="d", fanout=True)
        async def on_a(self, n: int) -> None: ...

    return S


def test_with_jetstream_off_the_description_has_no_streams_and_no_durable():
    """What the runtime does: it creates no stream and subscribes with no durable."""
    cls = _a_fanout_listener_with_a_durable_it_does_not_use()
    config = _jetstream_config(False)

    described = describe(cls, config=config)

    assert described.streams == []
    (item,) = described.listeners
    assert (item.durable, item.queue_group, item.fanout) == (None, None, True)
    assert _discovery_accepts(cls, config)


def test_CONTROL_with_jetstream_on_the_description_has_its_streams_and_its_durable():
    class Durable(CliffracerService):
        @listener("evt.a", durable="d")
        async def on_a(self, n: int) -> None: ...

    on = describe(Durable, config=_jetstream_config(True))
    assert [stream.name for stream in on.streams] == ["EVT"]
    assert (on.listeners[0].durable, on.listeners[0].queue_group) == ("d", "d")


def test_the_generator_exits_4_for_a_class_whose_listener_the_service_refuses(capsys, tmp_path):
    mod = tmp_path / "bad_listener_mod.py"
    mod.write_text(
        "from cliffracer import CliffracerService, listener, rpc\n"
        "class S(CliffracerService):\n"
        "    @rpc\n"
        "    async def ok(self, x: int) -> int:\n"
        "        return x\n"
        "    @listener('evt.a', fanout=True)\n"
        "    async def on_a(self, subject: str, **data) -> None: ...\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        rc = main(["--class", "bad_listener_mod:S", "--service", "s"])
    finally:
        sys.path.remove(str(tmp_path))

    assert rc == 4
    assert "S.on_a" in capsys.readouterr().err


def test_the_generator_exits_4_for_a_class_with_two_listeners_on_one_subject(capsys, tmp_path):
    mod = tmp_path / "dup_listener_mod.py"
    mod.write_text(
        "from cliffracer import CliffracerService, listener, rpc\n"
        "class S(CliffracerService):\n"
        "    @rpc\n"
        "    async def ok(self, x: int) -> int:\n"
        "        return x\n"
        "    @listener('evt.a', fanout=True)\n"
        "    async def first(self, n: int) -> None: ...\n"
        "    @listener('evt.a', fanout=True)\n"
        "    async def second(self, n: int) -> None: ...\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        rc = main(["--class", "dup_listener_mod:S", "--service", "s"])
    finally:
        sys.path.remove(str(tmp_path))

    assert rc == 4
    assert "Duplicate event listener" in capsys.readouterr().err
