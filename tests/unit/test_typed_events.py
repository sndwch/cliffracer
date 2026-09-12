"""Tests for strict event typing.

Verifies discovery-time signature inspection, refusal of untyped/variadic/bare
parameters with UntypedHandler, Pydantic model synthesis with extra="forbid",
and runtime validation with DLQ routing and message termination.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from cliffracer.core.decorators import broadcast, listener
from cliffracer.core.dispatch.dlq import DeadLetterPublisher
from cliffracer.core.dispatch.events import DispatchOutcome
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.typed_events import build_event_spec
from cliffracer.core.typed_rpc import UntypedHandler

pytestmark = pytest.mark.unit


class UserRegistered(BaseModel):
    user_id: str
    email: str


class DummyOwner:
    pass


def test_build_event_spec_single_model_parameter() -> None:
    async def on_user_registered(self: Any, event: UserRegistered) -> None:
        """Handle user registered."""

    spec = build_event_spec("on_user_registered", on_user_registered, owner=DummyOwner)
    assert spec.name == "on_user_registered"
    assert spec.is_single_model_param is True
    assert spec.single_model_param_name == "event"
    assert spec.payload_model is UserRegistered
    assert spec.takes_subject is False
    assert spec.takes_correlation_id is False
    assert spec.doc_summary == "Handle user registered."


def test_build_event_spec_synthesizes_forbid_model() -> None:
    async def on_payment(
        self: Any,
        account_id: str,
        amount: float,
        subject: str,
        correlation_id: str | None = None,
    ) -> None:
        pass

    spec = build_event_spec("on_payment", on_payment, owner=DummyOwner)
    assert spec.is_single_model_param is False
    assert spec.single_model_param_name is None
    assert spec.takes_subject is True
    assert spec.takes_correlation_id is True
    assert set(spec.payload_model.model_fields.keys()) == {"account_id", "amount"}
    assert spec.payload_model.model_config.get("extra") == "forbid"


def test_build_event_spec_zero_domain_parameters() -> None:
    async def on_heartbeat(self: Any, subject: str) -> None:
        pass

    spec = build_event_spec("on_heartbeat", on_heartbeat, owner=DummyOwner)
    assert spec.is_single_model_param is False
    assert spec.takes_subject is True
    assert len(spec.params) == 0
    assert len(spec.payload_model.model_fields) == 0


def test_build_event_spec_rejects_unannotated_parameter() -> None:
    async def bad_handler(self: Any, user_id) -> None:  # type: ignore[no-untyped-def]
        pass

    with pytest.raises(UntypedHandler, match="parameter 'user_id' has no annotation"):
        build_event_spec("bad_handler", bad_handler, owner=DummyOwner)


def test_build_event_spec_rejects_var_keyword() -> None:
    async def var_kw_handler(self: Any, **kwargs: Any) -> None:
        pass

    with pytest.raises(UntypedHandler, match=r"\*kwargs is not allowed on an event handler"):
        build_event_spec("var_kw_handler", var_kw_handler, owner=DummyOwner)


def test_build_event_spec_rejects_var_data() -> None:
    async def var_data_handler(self: Any, **data: Any) -> None:
        pass

    with pytest.raises(UntypedHandler, match=r"\*data is not allowed on an event handler"):
        build_event_spec("var_data_handler", var_data_handler, owner=DummyOwner)


def test_build_event_spec_rejects_var_positional() -> None:
    async def var_args_handler(self: Any, *args: Any) -> None:
        pass

    with pytest.raises(UntypedHandler, match=r"\*args is not allowed on an event handler"):
        build_event_spec("var_args_handler", var_args_handler, owner=DummyOwner)


def test_build_event_spec_rejects_positional_only() -> None:
    async def pos_only_handler(self: Any, user_id: str, /) -> None:
        pass

    with pytest.raises(UntypedHandler, match="positional-only parameter 'user_id' is not allowed"):
        build_event_spec("pos_only_handler", pos_only_handler, owner=DummyOwner)


def test_build_event_spec_rejects_bare_containers() -> None:
    async def bare_dict_handler(self: Any, payload: dict) -> None:
        pass

    with pytest.raises(UntypedHandler, match="parameter 'payload': dict is unsupported"):
        build_event_spec("bare_dict_handler", bare_dict_handler, owner=DummyOwner)

    async def bare_any_handler(self: Any, payload: Any) -> None:
        pass

    with pytest.raises(UntypedHandler, match="parameter 'payload': Any is unsupported"):
        build_event_spec("bare_any_handler", bare_any_handler, owner=DummyOwner)


def test_build_event_spec_rejects_invalid_subject_annotation() -> None:
    async def bad_subject_handler(self: Any, subject: int) -> None:
        pass

    with pytest.raises(UntypedHandler, match="parameter 'subject' must be annotated as str"):
        build_event_spec("bad_subject_handler", bad_subject_handler, owner=DummyOwner)


def test_build_event_spec_rejects_invalid_correlation_id_annotation() -> None:
    async def bad_cid_handler(self: Any, correlation_id: int) -> None:
        pass

    with pytest.raises(
        UntypedHandler, match="parameter 'correlation_id' annotation must be str or str | None"
    ):
        build_event_spec("bad_cid_handler", bad_cid_handler, owner=DummyOwner)


def test_build_event_spec_rejects_private_parameter_name() -> None:
    async def priv_handler(self: Any, _secret: str) -> None:
        pass

    with pytest.raises(UntypedHandler, match="parameter '_secret' starting with '_'"):
        build_event_spec("priv_handler", priv_handler, owner=DummyOwner)


def test_build_event_spec_rejects_mismatched_default() -> None:
    async def bad_default(self: Any, count: int = "not_an_int") -> None:  # type: ignore[assignment]
        pass

    with pytest.raises(
        UntypedHandler, match="default value 'not_an_int' for parameter 'count' does not match"
    ):
        build_event_spec("bad_default", bad_default, owner=DummyOwner)


def test_discovery_inspects_listener_and_broadcast() -> None:
    class SampleService(CliffracerService):
        @listener("events.user", fanout=True)
        async def on_user(self, event: UserRegistered) -> None:
            pass

        @broadcast("broadcast.alert")
        async def on_alert(self, message: str, level: int = 1) -> None:
            pass

    config = ServiceConfig(name="sample_svc")
    svc = SampleService(config)
    svc.container.discover_handlers()

    assert "on_user" in svc.container.registry.event_specs
    assert "on_alert" in svc.container.registry.event_specs
    assert "events.user" in svc.container.registry.event_specs_by_subject
    assert "broadcast.alert" in svc.container.registry.event_specs_by_subject


def test_discovery_fails_fast_on_untyped_listener() -> None:
    class UntypedService(CliffracerService):
        @listener("events.raw", fanout=True)
        async def on_raw(self, **data: Any) -> None:
            pass

    config = ServiceConfig(name="untyped_svc")
    svc = UntypedService(config)
    with pytest.raises(UntypedHandler, match=r"\*data is not allowed on an event handler"):
        svc.container.discover_handlers()


def test_discovery_fails_fast_on_untyped_broadcast() -> None:
    class UntypedBroadcastService(CliffracerService):
        @broadcast("alerts.raw")
        async def on_alert(self, **kwargs: Any) -> None:
            pass

    config = ServiceConfig(name="untyped_bc_svc")
    svc = UntypedBroadcastService(config)
    with pytest.raises(UntypedHandler, match=r"\*kwargs is not allowed on an event handler"):
        svc.container.discover_handlers()


@pytest.mark.asyncio
async def test_event_dispatcher_valid_single_model_dispatch() -> None:
    received: list[UserRegistered] = []

    class UserHandlerService(CliffracerService):
        @listener("users.registered", fanout=True)
        async def on_registered(self, event: UserRegistered) -> None:
            received.append(event)

    config = ServiceConfig(name="user_svc")
    svc = UserHandlerService(config)
    svc.container.discover_handlers()

    dispatcher = svc.container.event_dispatcher
    msg = AsyncMock()
    msg.subject = "users.registered"
    msg.data = b'{"user_id": "u-123", "email": "user@example.com"}'
    msg.headers = {"Content-Type": "application/json"}

    outcome = await dispatcher.handle_event(msg)
    assert outcome == DispatchOutcome.OK
    assert len(received) == 1
    assert received[0].user_id == "u-123"
    assert received[0].email == "user@example.com"


@pytest.mark.asyncio
async def test_event_dispatcher_valid_synthesized_model_dispatch() -> None:
    received_orders: list[tuple[str, float, str]] = []

    class OrderHandlerService(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_order(self, order_id: str, amount: float, subject: str) -> None:
            received_orders.append((order_id, amount, subject))

    config = ServiceConfig(name="order_svc")
    svc = OrderHandlerService(config)
    svc.container.discover_handlers()

    dispatcher = svc.container.event_dispatcher
    msg = AsyncMock()
    msg.subject = "orders.created"
    msg.data = b'{"order_id": "ord-99", "amount": 49.99}'
    msg.headers = {"Content-Type": "application/json"}

    outcome = await dispatcher.handle_event(msg)
    assert outcome == DispatchOutcome.OK
    assert len(received_orders) == 1
    assert received_orders[0] == ("ord-99", 49.99, "orders.created")


@pytest.mark.asyncio
async def test_event_dispatcher_invalid_payload_routes_to_dlq_and_safe_term() -> None:
    handled = False

    class StrictService(CliffracerService):
        @listener("accounts.opened", fanout=True)
        async def on_opened(self, account_id: str, initial_deposit: float) -> None:
            nonlocal handled
            handled = True

    config = ServiceConfig(name="strict_svc", dlq_subject="dlq.accounts")
    svc = StrictService(config)
    svc.container.discover_handlers()

    dispatcher = svc.container.event_dispatcher
    mock_dlq = AsyncMock(spec=DeadLetterPublisher)
    dispatcher.dlq = mock_dlq

    msg = AsyncMock()
    msg.subject = "accounts.opened"
    # extra field violates extra="forbid", and amount is wrong type
    msg.data = b'{"account_id": "acc-1", "initial_deposit": "invalid", "extra_bad_field": 123}'
    msg.headers = {"Content-Type": "application/json"}

    outcome = await dispatcher.handle_event(msg)
    assert outcome == DispatchOutcome.INVALID
    assert handled is False
    assert mock_dlq.handle_invalid_message.called
    assert msg.term.called
