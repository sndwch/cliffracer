"""Correlation propagates through the paths the framework spawns work on, not only `create_task`.

`asyncio.create_task` copying the creating context is the interpreter's guarantee, and a test of it
(`test_correlation_propagates_into_spawned_task`) cannot fail for a change to this framework. The
places it could regress are the event callback, which spawns a task per message and must run the
handler under the id the MESSAGE carries, and the lifecycle's supervised spawner, which must not
detach the task from the creator's context.
"""

import asyncio
import contextvars

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.correlation import CorrelationContext
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit

SEEN: list[str | None] = []


class Recorder(CliffracerService):
    @listener("evt.a", fanout=True)
    async def on_a(self, n: int) -> None:
        SEEN.append(CorrelationContext.get())


@pytest.fixture(autouse=True)
def _clean():
    SEEN.clear()
    CorrelationContext.clear()
    yield
    CorrelationContext.clear()


def _service(**config) -> Recorder:
    service = Recorder(ServiceConfig(name="recorder", health_port=0, **config))
    service._discover_handlers()
    return service


async def _settled(service: CliffracerService) -> None:
    tasks = list(service.container.lifecycle.active_tasks)
    assert tasks, "the callback spawned nothing, so there is nothing to wait for"
    await asyncio.gather(*tasks)


def _message(correlation_id: str | None) -> MockMessage:
    headers = {"Content-Type": "application/json"}
    if correlation_id is not None:
        headers["correlation_id"] = correlation_id
    return MockMessage("evt.a", data=b'{"n": 1}', headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [None, 4], ids=["unbounded", "bounded"])
async def test_the_handler_runs_under_the_id_the_message_carries_not_the_ambient_one(concurrency):
    service = _service(max_event_concurrency=concurrency)
    CorrelationContext.set("ambient-id")

    await service.container.dispatcher.make_event_callback("evt.a")(_message("message-id"))
    await _settled(service)

    assert SEEN == ["message-id"]
    assert CorrelationContext.get() == "ambient-id", "the callback's own context was changed"


@pytest.mark.asyncio
async def test_a_message_handled_in_the_callers_own_context_leaves_the_callers_id_as_it_was():
    """A spawned task runs in a copy, so a leak is invisible there; handled inline it is not."""
    service = _service()
    CorrelationContext.set("ambient-id")

    await service.container.dispatcher.handle_event(
        _message("message-id"), pattern="evt.a", raise_on_error=True
    )

    assert SEEN == ["message-id"]
    assert CorrelationContext.get() == "ambient-id"


@pytest.mark.asyncio
async def test_two_messages_in_flight_each_run_under_their_own_id():
    service = _service()
    callback = service.container.dispatcher.make_event_callback("evt.a")
    CorrelationContext.set("ambient-id")

    await callback(_message("first"))
    await callback(_message("second"))
    await _settled(service)

    assert sorted(i for i in SEEN if i) == ["first", "second"]


@pytest.mark.asyncio
async def test_a_message_with_no_id_does_not_run_under_a_stale_one_from_the_callback():
    service = _service()
    CorrelationContext.set("ambient-id")

    await service.container.dispatcher.make_event_callback("evt.a")(_message(None))
    await _settled(service)

    assert len(SEEN) == 1 and SEEN[0] and SEEN[0] != "ambient-id", SEEN


@pytest.mark.asyncio
async def test_the_supervised_spawner_keeps_the_creators_context_and_tracks_the_task():
    service = _service()
    seen: list[str | None] = []

    async def child() -> None:
        seen.append(CorrelationContext.get())

    CorrelationContext.set("trace-task")
    task = service.container.lifecycle.spawn_supervised_task(child(), name="probe")

    assert task in service.container.lifecycle.active_tasks
    await task
    assert seen == ["trace-task"]


@pytest.mark.asyncio
async def test_CONTROL_a_task_started_in_an_empty_context_does_not_see_the_id():
    """The instrument: the supervised-spawner test is red for a spawner that detaches the task."""
    seen: list[str | None] = []

    async def child() -> None:
        seen.append(CorrelationContext.get())

    CorrelationContext.set("trace-task")
    await asyncio.create_task(child(), context=contextvars.Context())

    assert seen != ["trace-task"]
