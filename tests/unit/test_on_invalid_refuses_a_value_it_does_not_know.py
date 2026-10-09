"""`on_invalid` accepts the two strategies it has, and refuses anything else.

Dispatch dead-letters only on exactly "deadletter" and drops on every other
value, so a plausible typo -- "dead-letter", "dlq", a trailing space -- used to
start cleanly and then discard every invalid message while the author believed
they were being kept. The service-wide `ServiceConfig.default_on_invalid` was
already constrained; the per-listener override was not. It is refused where it
is written, when the decorator runs, before any service exists.
"""

import pytest

from cliffracer import validated_listener
from cliffracer.core.exceptions import ConfigurationError
from tests.unit.test_validated_listener_dispatch import OrderCreated, _make_service, _MockMsg

pytestmark = pytest.mark.unit

TYPOS = ["dead-letter", "deadLetter", "dlq", "deadletter ", "DROP", ""]


@pytest.mark.parametrize("value", TYPOS, ids=repr)
def test_an_unknown_on_invalid_is_refused_when_the_decorator_runs(value):
    with pytest.raises(ConfigurationError) as refused:
        validated_listener("orders.created", OrderCreated, on_invalid=value, fanout=True)

    message = str(refused.value)
    assert "@validated_listener" in message, message
    assert repr(value) in message, message
    assert "'deadletter'" in message and "'drop'" in message, message


@pytest.mark.parametrize(
    ("value", "dead_letters"),
    [("deadletter", 1), ("drop", 0), (None, 1)],
    ids=["deadletter", "drop", "service default"],
)
async def test_CONTROL_each_accepted_value_still_does_what_it_names(value, dead_letters):
    """With the service default at "deadletter", so None and "drop" differ."""
    svc = _make_service(on_invalid=value, default="deadletter")

    await svc.container._handle_event(_MockMsg("orders.created", {"order_id": "o1"}))

    assert svc.container._publish_dlq.await_count == dead_letters
    assert svc.received == []
