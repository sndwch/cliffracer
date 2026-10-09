"""`default_calls` and `default_window` limit the dispatches a caller sends, and nothing else.

A `describe` request is answered with no authentication and is what a client's `verify=True` and
the generator read; a timer or cron firing has no caller. Neither is a handler a limit was written
for, and a default that counted them refused a client that checked the contract once the window was
spent and throttled a schedule by its own ticks.
"""

import json

import pytest
from cliffracer_resilience import ResilienceExtension, rate_limit

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.core.extension import RejectMessage, WorkerContext
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

CALLS = 2


class Defaulted(CliffracerService):
    resilience = ResilienceExtension(default_calls=CALLS, default_window=60.0)

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="defaulted", subject_prefix=None))

    @rpc
    async def plain(self) -> int:
        return 1

    @rpc
    @rate_limit(calls=100, window=60.0)
    async def own(self) -> int:
        return 1


async def _started() -> Defaulted:
    service = Defaulted()
    await service.container._setup_extensions()
    service._discover_handlers()
    return service


def _context(kind: str, handler: str | None) -> WorkerContext:
    data = {"handler_name": handler} if handler else {}
    return WorkerContext(
        kind=kind,
        subject=f"defaulted.{handler}",
        headers={},
        correlation_id="c",
        payload={},
        data=data,
    )


async def _describe(service: Defaulted) -> dict:
    message = MockMessage("defaulted.describe", reply="_INBOX.d")
    await service.container._handle_describe_request(message)
    assert message.responded_data is not None
    return json.loads(message.responded_data)


async def _admitted(service: Defaulted, kind: str, handler: str | None) -> bool:
    try:
        await service.resilience.worker_setup(_context(kind, handler))
    except RejectMessage:
        return False
    return True


async def test_every_describe_is_answered_however_often_it_is_asked():
    service = await _started()

    replies = [await _describe(service) for _ in range(CALLS + 3)]

    assert all("methods" in reply for reply in replies), replies
    assert "unknown" not in service.resilience.health_details()["rate_limits"]["by_handler"]


@pytest.mark.parametrize("kind", ["timer", "describe"])
async def test_a_dispatch_that_no_caller_sent_is_never_limited_by_the_default(kind):
    service = await _started()

    admitted = [await _admitted(service, kind, "tick") for _ in range(CALLS + 3)]

    assert admitted == [True] * (CALLS + 3)
    assert service.resilience.health_details()["rate_limits"]["by_handler"] == {}


@pytest.mark.parametrize("kind", ["rpc", "async_rpc", "event"])
async def test_CONTROL_a_dispatch_a_caller_sent_is_limited_by_the_default(kind):
    service = await _started()

    admitted = [await _admitted(service, kind, "plain") for _ in range(CALLS + 2)]

    assert admitted == [True] * CALLS + [False, False]
    assert service.resilience.health_details()["rate_limits"]["by_handler"]["plain"] == {
        "permitted": CALLS,
        "refused": 2,
    }


async def test_CONTROL_a_limit_a_handler_declares_still_applies_to_a_timer_dispatch():
    """Only the default is restricted by kind: a handler that names its own limit has asked for it."""
    service = await _started()
    service.resilience._rate_limits["own"] = service.resilience._rate_limits["own"].__class__(
        calls=1, window=60.0
    )

    admitted = [await _admitted(service, "timer", "own") for _ in range(3)]

    assert admitted == [True, False, False]


class Listens(CliffracerService):
    resilience = ResilienceExtension(default_calls=1, default_window=60.0)

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="listens", subject_prefix=None))

    @listener("orders.created", fanout=True)
    async def on_created(self, order_id: str = "") -> None:
        return None


async def test_CONTROL_the_default_limits_an_event_listener_through_the_container():
    service = Listens()
    await service.container._setup_extensions()
    service._discover_handlers()

    outcomes = []
    for _ in range(3):
        message = MockMessage("orders.created", data=b'{"order_id": "o"}', reply=None)
        await service.container._handle_event(message)
        outcomes.append(service.resilience.health_details()["rate_limits"]["by_handler"])

    assert outcomes[-1]["on_created"] == {"permitted": 1, "refused": 2}
