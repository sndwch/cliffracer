"""A rejected event names the one rule it broke, and a message with no `term` is left alone.

The dispatcher test for an invalid payload sent a body that broke two rules at once (a wrong
type and an undeclared field), so it could not say which one fired: turning the synthesized
model's `extra="forbid"` into `"ignore"` left it green, because the wrong type still failed.
Each rule gets its own body here, and the dead-letter call is read for what it was handed.

`safe_term` and its siblings guard on the message having the method, because a core-NATS message
has no `term`, `ack`, `nak` or `in_progress`; that guard is the difference between returning False
quietly and logging a failure for a call that was never possible.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from cliffracer.core.decorators import listener
from cliffracer.core.dispatch import EventDispatcher, JetStreamDispatcher
from cliffracer.core.dispatch.dlq import DeadLetterPublisher
from cliffracer.core.dispatch.events import DispatchOutcome
from cliffracer.core.dispatch.pipeline import ExtensionPipeline
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit

SUBJECT = "accounts.opened"


def _service() -> tuple[CliffracerService, list[str], AsyncMock]:
    handled: list[str] = []

    class Accounts(CliffracerService):
        @listener(SUBJECT, fanout=True)
        async def on_opened(self, account_id: str, initial_deposit: float) -> None:
            handled.append(account_id)

    svc = Accounts(ServiceConfig(name="accounts_svc", dlq_subject="dlq.accounts"))
    svc.container.discover_handlers()
    dlq = AsyncMock(spec=DeadLetterPublisher)
    svc.container.event_dispatcher.dlq = dlq
    return svc, handled, dlq


def _message(body: bytes) -> AsyncMock:
    msg = AsyncMock()
    msg.subject = SUBJECT
    msg.data = body
    msg.headers = {"Content-Type": "application/json"}
    return msg


def _routed(dlq: AsyncMock) -> dict[str, Any]:
    """What the dispatcher handed the dead-letter publisher, by parameter name."""
    dlq.handle_invalid_message.assert_awaited_once()
    call = dlq.handle_invalid_message.await_args
    bound = inspect.signature(DeadLetterPublisher.handle_invalid_message).bind(
        dlq, *call.args, **call.kwargs
    )
    return dict(list(bound.arguments.items())[1:])


async def _reject(body: bytes) -> tuple[DispatchOutcome, dict[str, Any], list[str], AsyncMock]:
    svc, handled, dlq = _service()
    msg = _message(body)
    outcome = await svc.container.event_dispatcher.handle_event(msg)
    spec = svc.container.registry.event_specs_by_subject[SUBJECT]
    routed = _routed(dlq)
    assert routed["schema"] is spec.payload_model
    assert routed["subject"] == SUBJECT
    return outcome, routed, handled, msg


def _kinds(routed: dict[str, Any]) -> dict[tuple, str]:
    return {tuple(e["loc"]): e["type"] for e in routed["error"].errors()}


async def test_a_wrong_type_alone_is_refused_for_that_field_only():
    outcome, routed, handled, _ = await _reject(
        b'{"account_id": "acc-1", "initial_deposit": "invalid"}'
    )

    assert outcome == DispatchOutcome.INVALID
    assert handled == []
    assert set(_kinds(routed)) == {("initial_deposit",)}
    assert _kinds(routed)[("initial_deposit",)] != "extra_forbidden"


async def test_an_undeclared_field_alone_is_refused_though_every_declared_field_is_right():
    outcome, routed, handled, _ = await _reject(
        b'{"account_id": "acc-1", "initial_deposit": 10.0, "extra_bad_field": 123}'
    )

    assert outcome == DispatchOutcome.INVALID
    assert handled == []
    assert _kinds(routed) == {("extra_bad_field",): "extra_forbidden"}
    assert routed["payload"] == {
        "account_id": "acc-1",
        "initial_deposit": 10.0,
        "extra_bad_field": 123,
    }


async def test_CONTROL_both_faults_together_are_both_reported():
    outcome, routed, _, _ = await _reject(
        b'{"account_id": "acc-1", "initial_deposit": "invalid", "extra_bad_field": 123}'
    )

    assert outcome == DispatchOutcome.INVALID
    assert set(_kinds(routed)) == {("initial_deposit",), ("extra_bad_field",)}


async def test_CONTROL_a_valid_event_is_handled_and_dead_letters_nothing():
    svc, handled, dlq = _service()

    outcome = await svc.container.event_dispatcher.handle_event(
        _message(b'{"account_id": "acc-1", "initial_deposit": 10.0}')
    )

    assert outcome == DispatchOutcome.OK
    assert handled == ["acc-1"]
    dlq.handle_invalid_message.assert_not_awaited()


def _jetstream() -> JetStreamDispatcher:
    cfg = ServiceConfig(name="js_svc")
    dlq = DeadLetterPublisher(cfg, lambda: MagicMock())
    events = EventDispatcher(ServiceRegistry(), cfg, ExtensionPipeline([]), dlq)
    return JetStreamDispatcher(cfg, lambda: MagicMock(), events, dlq)


@pytest.mark.parametrize("method", ["safe_ack", "safe_nak", "safe_term", "safe_in_progress"])
async def test_a_message_without_the_broker_method_is_left_alone_and_nothing_is_logged(method):
    """A core-NATS message has none of ack, nak, term, in_progress: the answer is False, and it
    is not a failure, so nothing is logged at any level for a call that was never made."""
    logged: list[str] = []
    sink = logger.add(lambda m: logged.append(m.record["message"]), level="DEBUG")
    try:
        result = await getattr(_jetstream(), method)(SimpleNamespace(subject="plain.core"))
    finally:
        logger.remove(sink)

    assert result is False
    assert logged == []


async def test_CONTROL_a_message_whose_term_raises_is_reported_as_a_logged_failure():
    """The same False, for the opposite reason: the call was made and failed, so it is logged."""
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    msg = SimpleNamespace(subject="js.subject", term=AsyncMock(side_effect=OSError("net down")))
    try:
        result = await _jetstream().safe_term(msg)
    finally:
        logger.remove(sink)

    assert result is False
    assert len(warnings) == 1 and "Failed to TERM" in warnings[0]
