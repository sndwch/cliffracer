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
from cliffracer.core.discovery import HandlerDiscovery

pytestmark = pytest.mark.unit


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
    # shapes the list above never had: tokens with an underscore, a digit, a version, a wildcard
    # first, and a bare `>`
    "orders.order_created",
    "payments.payment_completed",
    "events.complex.v1",
    "live82.pull.events",
    "burst.orders.>",
    "*.created",
    "*.*",
    ">",
]


@pytest.mark.parametrize("subject", SUBJECTS_IN_USE)
def test_subjects_already_in_use_are_still_accepted(subject):
    listener(subject)
    broadcast(subject)
    validated_listener(subject, Evt)


def _subjects_the_tree_declares() -> set[str]:
    """Every string literal passed first to `listener`, `broadcast` or `validated_listener` in the
    tree, found by reading the code rather than by someone remembering to add it to a list.

    Calls inside a `with pytest.raises(...)` are left out: they are subjects a test means to refuse.
    """
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    roots = [
        repo / "src",
        repo / "examples",
        repo / "tests",
        *repo.glob("packages/*/src"),
        *repo.glob("packages/*/tests"),
    ]
    found: set[str] = set()
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            refused = {
                id(node)
                for with_ in ast.walk(tree)
                if isinstance(with_, ast.With)
                and any(
                    isinstance(item.context_expr, ast.Call)
                    and getattr(item.context_expr.func, "attr", "") == "raises"
                    for item in with_.items
                )
                for node in ast.walk(with_)
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or id(node) in refused:
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if (
                    name in {"listener", "broadcast", "validated_listener"}
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    found.add(node.args[0].value)
    return found


def test_every_subject_the_tree_declares_is_accepted():
    """The list above is a sample; this is the tree. A validator tightened until it refuses a
    subject the code really uses fails here and names it, instead of surfacing as a collection
    error in an unrelated file."""
    subjects = _subjects_the_tree_declares()
    assert len(subjects) > 50, (
        f"the walk found only {len(subjects)} subjects: it is not reading the tree"
    )

    for subject in sorted(subjects):
        try:
            listener(subject)
            broadcast(subject)
            validated_listener(subject, Evt)
        except ConfigurationError as exc:
            pytest.fail(f"the tree declares {subject!r}, which the validator refuses: {exc}")


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


def test_a_message_subclass_is_refused_too():
    """BroadcastMessage is the class the original misuse actually passed."""
    with pytest.raises(ConfigurationError) as exc:
        broadcast(BroadcastMessage)

    assert "BroadcastMessage" in str(exc.value)


@pytest.mark.parametrize("subject", [42, None, ["orders.created"], object()])
def test_other_non_strings_are_refused(subject):
    with pytest.raises(ConfigurationError) as exc:
        listener(subject)

    assert "subject string" in str(exc.value)


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


def test_the_error_arrives_at_class_definition_not_at_discovery():
    """Decoration time is where the mistake is; discovery is already too late."""
    with pytest.raises(ConfigurationError):

        class _S(CliffracerService):
            @listener(Evt, fanout=True)
            async def on_thing(self, subject: str) -> None:
                pass


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

    registry = svc.container.registry
    published = HandlerDiscovery.with_namespace(svc.config, "system.alerts")
    assert sorted(registry.event_handlers) == ["app1.orders.created", "app1.system.alerts"]
    assert published == "app1.system.alerts"
    assert registry.broadcast_handlers.keys() == {published}


def test_validation_does_not_reject_a_wildcard_only_subject():
    """The validator says nothing about wildcard placement, deliberately."""
    listener("*")
    listener(">")
    listener("*.orders.created")
