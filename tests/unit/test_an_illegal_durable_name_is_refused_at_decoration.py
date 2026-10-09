"""A durable consumer name the broker will refuse is refused when the handler is declared.

`durable=` is recorded as given and `validate_unique_durables` checks only that two handlers do
not share one, so a name with a `.` in it travelled from the decorator through discovery to
`_setup_subscriptions`, where the broker refused it after the connection was up and the RPC
subscriptions were live, in words that name neither the handler nor the decorator. Subjects get
the refusal at decoration time; the durable beside them did not.

The rule is what a broker was measured to refuse (nats-server 2.10 through nats-py): `.`, `*`, `>`,
`/`, `\\` and whitespace, and a name longer than 255 characters. `-`, `_`, `:`, `,`, digits, capitals
and non-ASCII letters are accepted, so they are accepted here.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig, listener
from cliffracer.core.decorators import validated_listener

pytestmark = pytest.mark.unit


class Order(BaseModel):
    order_id: str


ILLEGAL = [
    ("a.b", "."),
    ("a*b", "*"),
    ("a>b", ">"),
    ("a/b", "/"),
    ("a\\b", "\\"),
    ("a b", "whitespace"),
    ("a\tb", "whitespace"),
    ("a\nb", "whitespace"),
    ("bad.durable.name", "."),
]


def _declare_listener(durable):
    @listener("orders.created", durable=durable)
    async def on_created(self, order_id: str) -> None: ...

    return on_created


def _declare_validated(durable):
    @validated_listener("orders.created", Order, durable=durable)
    async def on_created(self, message: Order) -> None: ...

    return on_created


DECLARE = pytest.mark.parametrize(
    "declare", [_declare_listener, _declare_validated], ids=["listener", "validated_listener"]
)


@DECLARE
@pytest.mark.parametrize(("name", "why"), ILLEGAL, ids=[repr(n) for n, _ in ILLEGAL])
def test_an_illegal_durable_name_is_refused_naming_the_handler_and_the_fault(declare, name, why):
    with pytest.raises(ConfigurationError) as caught:
        declare(name)

    message = str(caught.value)
    assert "on_created" in message, message
    assert repr(name) in message, message
    assert why in message, message


@DECLARE
def test_a_durable_name_over_255_characters_is_refused(declare):
    with pytest.raises(ConfigurationError, match="255"):
        declare("a" * 256)


@DECLARE
def test_a_durable_name_that_is_not_a_string_is_refused(declare):
    with pytest.raises(ConfigurationError, match="str"):
        declare(7)


@DECLARE
@pytest.mark.parametrize(
    "name",
    ["ok_name-1", "UPPER", "a:b", "a,b", "é", "9", "-lead", "a" * 255, "pdf-extractor"],
)
def test_CONTROL_a_name_the_broker_accepts_is_accepted(declare, name):
    handler = declare(name)

    assert handler._cliffracer_event_durables == {"orders.created": name}


@DECLARE
@pytest.mark.parametrize("absent", [None, ""])
def test_CONTROL_no_durable_is_still_no_durable(declare, absent):
    handler = declare(absent)

    assert not getattr(handler, "_cliffracer_event_durables", {})


def test_the_refusal_is_at_import_time_not_when_the_service_starts():
    """The class body is where the decorator runs: the service is never built."""
    with pytest.raises(ConfigurationError, match=r"'x\.y'"):

        class Orders(CliffracerService):
            @listener("orders.created", durable="x.y")
            async def on_created(self, order_id: str) -> None: ...

    # And a legal durable on the same shape builds and discovers.
    class Fine(CliffracerService):
        @listener("orders.created", durable="x-y")
        async def on_created(self, order_id: str) -> None: ...

    svc = Fine(ServiceConfig(name="fine", jetstream_enabled=True, health_port=0))
    svc._discover_handlers()
    assert svc.container.registry.event_durables == {"orders.created": "x-y"}
