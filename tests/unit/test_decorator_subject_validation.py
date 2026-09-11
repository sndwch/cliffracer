"""Tests ensuring subject-taking decorators validate subject strings at decoration time."""

import pytest
from pydantic import BaseModel

from cliffracer import (
    BroadcastMessage,
    CliffracerService,
    ConfigurationError,
    ServiceConfig,
    broadcast,
    listener,
    validated_listener,
)


class Evt(BaseModel):
    id: str


# Every subject shape actually used across src/, tests/ and examples/. This is a
# regression guard on the validator itself: a checker that rejects working
# subjects is worse than no checker.
SUBJECTS_IN_USE = [
    "events.*",
    "logs.>",
    "system.>",
    "users.*.created",
    "orders.created",
    "orders.events.*",
    "system.alerts",
    "system.alerts.*",
    "itest.events.ping",
    "user.events",
    "user.events.*",
    "events.extraction.completed",
    "ping.events",
]


@pytest.mark.unit
@pytest.mark.parametrize("subject", SUBJECTS_IN_USE)
def test_subjects_already_in_use_are_still_accepted(subject):
    listener(subject)
    broadcast(subject)
    validated_listener(subject, Evt)


@pytest.mark.unit
@pytest.mark.parametrize(
    "decorator,call",
    [
        ("listener", lambda: listener(Evt)),
        ("broadcast", lambda: broadcast(Evt)),
        ("validated_listener", lambda: validated_listener(Evt, Evt)),
    ],
)
def test_a_model_class_is_refused_and_points_at_validated_listener(decorator, call):
    """The instinct is right; the decorator is wrong. Say where to go."""
    with pytest.raises(ConfigurationError) as exc:
        call()

    message = str(exc.value)
    assert f"@{decorator}" in message
    assert "Evt" in message
    assert "validated_listener" in message


@pytest.mark.unit
def test_a_message_subclass_is_refused_too():
    """BroadcastMessage is the class the original misuse actually passed."""
    with pytest.raises(ConfigurationError) as exc:
        broadcast(BroadcastMessage)

    assert "BroadcastMessage" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize("subject", [42, None, ["orders.created"], object()])
def test_other_non_strings_are_refused(subject):
    with pytest.raises(ConfigurationError) as exc:
        listener(subject)

    assert "subject string" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "subject,reason",
    [
        ("", "empty"),
        ("orders created", "whitespace"),
        ("orders\tcreated", "whitespace"),
        ("orders..created", "empty token"),
        (".orders", "empty token"),
        ("orders.", "empty token"),
    ],
)
def test_a_subject_nats_would_refuse_is_caught_here_instead(subject, reason):
    """`nats: invalid subject` at connect becomes unreachable from a decorator."""
    with pytest.raises(ConfigurationError) as exc:
        listener(subject)

    message = str(exc.value)
    assert reason in message
    assert repr(subject) in message


@pytest.mark.unit
def test_the_error_arrives_at_class_definition_not_at_discovery():
    """Decoration time is where the mistake is; discovery is already too late."""
    with pytest.raises(ConfigurationError):

        class _S(CliffracerService):
            @listener(Evt, fanout=True)
            async def on_thing(self, subject: str) -> None:
                pass


@pytest.mark.unit
def test_a_valid_service_still_builds_and_registers():
    """The guard must not disturb the ordinary path."""

    class S(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(self, subject: str) -> None:
            pass

        @broadcast("system.alerts")
        async def on_alert(self, subject: str) -> None:
            pass

    svc = S(ServiceConfig(name="svc", namespace="app1"))
    svc._discover_handlers()

    assert "app1.orders.created" in svc.container.registry.event_handlers
    assert all(isinstance(key, str) for key in svc.container.registry.event_handlers)


@pytest.mark.unit
def test_validation_does_not_reject_a_wildcard_only_subject():
    """The validator says nothing about wildcard placement, deliberately."""
    listener("*")
    listener(">")
    listener("*.orders.created")
