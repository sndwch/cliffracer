"""Unit tests verifying that listeners explicitly declare either fanout=True or a durable consumer."""

import pytest

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig, listener, rpc

pytestmark = pytest.mark.unit


def _config(**overrides):
    return ServiceConfig(name="svc", namespace="ns", **overrides)


def test_a_listener_with_neither_durable_nor_fanout_is_refused():
    class S(CliffracerService):
        @listener("events.thing.happened")
        async def on_thing(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config())._discover_handlers()

    message = str(exc.value)
    assert "on_thing" in message, "the error must name the handler"
    assert "events.thing.happened" in message, "the error must name the subject"
    assert "fanout=True" in message, "the error must name the way out"
    assert "durable" in message
    # The branch that ran: every refusal ends with the fanout=True sentence, so the line above
    # cannot tell this diagnosis from the inert-durable one.
    assert "declare neither a durable nor fanout" in message, message
    assert "inert" not in message, message


def test_declaring_fanout_is_accepted_and_keeps_the_old_behaviour():
    class S(CliffracerService):
        @listener("events.thing.happened", fanout=True)
        async def on_thing(self, subject: str) -> None:
            pass

    svc = S(_config())
    svc._discover_handlers()

    subject = "ns.events.thing.happened"
    assert subject in svc.container.registry.event_handlers
    assert subject not in svc.container.registry.event_durables, (
        "fan-out must not acquire a durable"
    )


def test_a_durable_listener_still_needs_no_flag():
    class S(CliffracerService):
        @listener("events.thing.happened", durable="thing-happened")
        async def on_thing(self, subject: str) -> None:
            pass

    svc = S(_config(jetstream_enabled=True))
    svc._discover_handlers()

    assert svc.container.registry.event_durables["ns.events.thing.happened"] == "thing-happened"


def test_declaring_both_is_refused_because_they_mean_opposite_things():
    """A durable is one-replica-per-message; fanout is every-replica. Not both."""

    class S(CliffracerService):
        @listener("events.thing.happened", durable="d", fanout=True)
        async def on_thing(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config(jetstream_enabled=True))._discover_handlers()

    assert "fanout" in str(exc.value)
    assert "durable" in str(exc.value)


def test_rpc_handlers_are_untouched():
    """RPC is queue-grouped by the framework; this guard must not reach it."""

    class S(CliffracerService):
        @rpc
        async def work(self, n: int = 0) -> int:
            return n

    svc = S(_config())
    svc._discover_handlers()
    assert "work" in svc.container.registry.rpc_handlers


def test_the_refusal_says_what_actually_happens_at_two_replicas():
    class S(CliffracerService):
        @listener("events.thing.happened")
        async def on_thing(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config())._discover_handlers()

    message = str(exc.value)
    assert "every replica" in message.lower()


def test_a_validated_listener_with_neither_is_refused_too():
    from pydantic import BaseModel

    from cliffracer import validated_listener

    class Payload(BaseModel):
        n: int = 0

    class S(CliffracerService):
        @validated_listener("events.thing.happened", Payload)
        async def on_thing(self, message: Payload) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config())._discover_handlers()

    message = str(exc.value)
    assert "on_thing" in message
    assert "fanout=True" in message
    assert "declare neither a durable nor fanout" in message, message


def test_a_validated_listener_may_declare_fanout():
    from pydantic import BaseModel

    from cliffracer import validated_listener

    class Payload(BaseModel):
        n: int = 0

    class S(CliffracerService):
        @validated_listener("events.thing.happened", Payload, fanout=True)
        async def on_thing(self, message: Payload) -> None:
            pass

    svc = S(_config())
    svc._discover_handlers()
    assert "ns.events.thing.happened" in svc.container.registry.event_handlers


def test_a_broadcast_handler_needs_no_flag():
    """Verify that @broadcast handlers implicitly fan out without requiring fanout=True."""
    from cliffracer import broadcast

    class S(CliffracerService):
        @broadcast("system.alerts")
        async def on_alert(self, subject: str) -> None:
            pass

    svc = S(_config())
    svc._discover_handlers()  # must not raise
    assert "ns.system.alerts" in svc.container.registry.event_handlers


def test_a_broadcast_handler_is_recorded_as_fanout():
    """A `@broadcast` handler lands in `registry.event_fanout`, which is what the declaration
    rule reads; the test above checks the same handler is registered as an event handler."""
    from cliffracer import CliffracerService, broadcast

    class S(CliffracerService):
        @broadcast("system.alerts")
        async def on_alert(self, subject: str) -> None:
            pass

    svc = S(_config())
    svc._discover_handlers()  # must not raise
    assert "ns.system.alerts" in svc.container.registry.event_fanout


def test_a_durable_without_jetstream_does_not_count_as_a_declaration():
    """Verify durable listeners require fanout=True when JetStream is disabled."""

    class S(CliffracerService):
        @listener("orders.created", durable="orders")
        async def on_order(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config(jetstream_enabled=False))._discover_handlers()

    message = str(exc.value)
    assert "fanout=True" in message
    # The branch that ran. The fanout=True sentence ends every refusal, so only the inert-durable
    # diagnosis names the durable and says why it does not count.
    assert "declares durable 'orders', which is inert while jetstream_enabled is False" in message
    assert "declare neither a durable nor fanout" not in message, message


def test_a_durable_plus_fanout_is_allowed_when_jetstream_is_off():
    """Verify fanout=True is accepted for durable listeners when JetStream is disabled."""

    class S(CliffracerService):
        @listener("orders.created", durable="orders", fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

    svc = S(_config(jetstream_enabled=False))
    svc._discover_handlers()  # must not raise
    assert "ns.orders.created" in svc.container.registry.event_fanout


def test_a_durable_with_jetstream_on_is_still_fine():
    class S(CliffracerService):
        @listener("orders.created", durable="orders")
        async def on_order(self, subject: str) -> None:
            pass

    svc = S(_config(jetstream_enabled=True))
    svc._discover_handlers()  # must not raise
    assert svc.container.registry.event_durables["ns.orders.created"] == "orders"


def test_a_durable_with_jetstream_off_is_diagnosed_as_such():
    class S(CliffracerService):
        @listener("events.thing.happened", durable="thing-worker")
        async def on_thing(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config())._discover_handlers()

    message = str(exc.value)
    assert "thing-worker" in message, "the error must name the durable that was declared"
    assert "jetstream_enabled" in message, "the error must name the setting that made it inert"
    assert "declares neither a durable nor fanout" not in message, message
    assert "jetstream_enabled=True" in message, message


def test_CONTROL_a_listener_that_really_declared_neither_still_says_so():
    """Verify error message correctly indicates when neither durable nor fanout is declared."""

    class S(CliffracerService):
        @listener("events.thing.happened")
        async def on_thing(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config())._discover_handlers()

    message = str(exc.value)
    assert "declares neither a durable nor fanout" in message, message
    assert "jetstream_enabled is False" not in message, message


def test_a_mixed_service_diagnoses_each_listener_separately():
    """Verify mixed configuration errors are reported individually per listener."""

    class S(CliffracerService):
        @listener("events.with.durable", durable="thing-worker")
        async def on_with_durable(self, subject: str) -> None:
            pass

        @listener("events.without.anything")
        async def on_without(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config())._discover_handlers()

    message = str(exc.value)
    assert "thing-worker" in message and "jetstream_enabled is False" in message, message
    assert "declares neither a durable nor fanout" in message, message
    assert "2 listener(s)" in message, message


def test_a_compliant_listener_on_the_same_service_is_not_accused():
    """The refusal counts and lists the OFFENDERS, not every listener the service has.

    The mixed-service test above has two offenders, so a listing over every handler looks the
    same as a listing over the offenders. Here five listeners declare fanout and one does not.
    """

    class S(CliffracerService):
        @listener("events.fine.one", fanout=True)
        async def on_one(self, subject: str) -> None:
            pass

        @listener("events.fine.two", fanout=True)
        async def on_two(self, subject: str) -> None:
            pass

        @listener("events.fine.three", fanout=True)
        async def on_three(self, subject: str) -> None:
            pass

        @listener("events.fine.four", fanout=True)
        async def on_four(self, subject: str) -> None:
            pass

        @listener("events.fine.five", fanout=True)
        async def on_five(self, subject: str) -> None:
            pass

        @listener("events.the.offender")
        async def on_offender(self, subject: str) -> None:
            pass

    with pytest.raises(ConfigurationError) as exc:
        S(_config())._discover_handlers()

    message = str(exc.value)
    assert "1 listener(s)" in message, message
    assert "events.the.offender" in message, message
    assert "events.fine" not in message, message
