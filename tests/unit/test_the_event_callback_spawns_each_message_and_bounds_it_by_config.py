"""The event callback hands each message to the service's spawner, and `max_event_concurrency` bounds it.

What holds, each pinned against a literal and not against the constant it reads:

- A `task_spawner`, when the dispatcher has one, is what makes the task: it is called with the
  dispatch coroutine and the name `event:<pattern>` (`event_bounded:<pattern>` under a limit), and the
  task it returns is the one that runs the message. A dispatcher built without one still runs the
  handler, on a plain `asyncio.create_task`.
- The task a callback spawns logs a handler's failure and does not raise it: an exception out of the
  task would be a "Task exception was never retrieved" for every failing message.
- `get_event_semaphore` is `None` for no limit, and for a limit of `n` one semaphore with `n` permits,
  made once. A limit that is not above zero makes no semaphore: `Semaphore(0)` would wait forever.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cliffracer.core.dispatch import DeadLetterPublisher, EventDispatcher, ExtensionPipeline
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit

# A fail-safe that turns a handler that never ran into a legible failure, not a measurement.
HANDLER_RUNS_WITHIN = 2.0


def _message(subject: str = "evt.a") -> SimpleNamespace:
    return SimpleNamespace(subject=subject, data=b'{"n": 1}', headers={})


def _dispatcher(handler, *, limit: int | None = None, spawner=None) -> EventDispatcher:
    registry = ServiceRegistry()
    registry.event_handlers["evt.a"] = handler
    config = ServiceConfig(name="events_svc", health_port=0, max_event_concurrency=limit)
    dlq = DeadLetterPublisher(
        config,
        lambda: MagicMock(nc=MagicMock(publish=AsyncMock()), js=None, jetstream_active=False),
    )
    return EventDispatcher(registry, config, ExtensionPipeline([]), dlq, task_spawner=spawner)


async def _settle() -> None:
    """Let every task the callback spawned finish, so nothing outlives the test."""
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if others:
        await asyncio.wait_for(asyncio.gather(*others, return_exceptions=True), 5.0)


LIMITS = [
    pytest.param(None, "event:evt.a", id="unbounded"),
    pytest.param(2, "event_bounded:evt.a", id="bounded"),
]


@pytest.mark.parametrize(("limit", "expected_name"), LIMITS)
async def test_the_task_is_made_by_the_services_spawner_under_the_name_of_its_pattern(
    limit, expected_name
):
    seen: list[int] = []

    def handler(n: int) -> None:
        seen.append(n)

    spawned: list[tuple[str | None, asyncio.Task]] = []

    def spawner(coro, name):
        task = asyncio.create_task(coro, name=name)
        spawned.append((name, task))
        return task

    dispatcher = _dispatcher(handler, limit=limit, spawner=spawner)

    await dispatcher.make_event_callback("evt.a")(_message())
    await _settle()

    assert [name for name, _ in spawned] == [expected_name]
    assert seen == [1], "fixture: the spawned task must have run the handler"


@pytest.mark.parametrize("limit", [None, 2], ids=["unbounded", "bounded"])
async def test_a_dispatcher_with_no_spawner_still_runs_the_handler(limit):
    ran = asyncio.Event()

    def handler(n: int) -> None:
        ran.set()

    dispatcher = _dispatcher(handler, limit=limit, spawner=None)

    await dispatcher.make_event_callback("evt.a")(_message())
    await asyncio.wait_for(ran.wait(), HANDLER_RUNS_WITHIN)
    await _settle()

    assert ran.is_set()


@pytest.mark.parametrize(("limit", "expected_name"), LIMITS)
async def test_a_failing_handler_is_logged_and_does_not_fail_the_task_it_ran_in(
    limit, expected_name
):
    called: list[int] = []

    def handler(n: int) -> None:
        called.append(n)
        raise RuntimeError("the handler failed")

    tasks: list[asyncio.Task] = []

    def spawner(coro, name):
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    dispatcher = _dispatcher(handler, limit=limit, spawner=spawner)

    await dispatcher.make_event_callback("evt.a")(_message())
    await asyncio.wait_for(asyncio.wait(tasks), HANDLER_RUNS_WITHIN)

    assert called == [1], "fixture: the handler must have run, and failed"
    assert [task.exception() for task in tasks] == [None]


@pytest.mark.parametrize("limit", [1, 2])
def test_a_limit_gives_one_semaphore_made_once(limit):
    dispatcher = _dispatcher(lambda: None, limit=limit)

    semaphore = dispatcher.get_event_semaphore()

    assert semaphore is not None
    assert dispatcher.get_event_semaphore() is semaphore, "a second call made a second semaphore"


@pytest.mark.parametrize("limit", [1, 2])
async def test_the_semaphore_runs_out_after_exactly_the_configured_number_of_permits(limit):
    semaphore = _dispatcher(lambda: None, limit=limit).get_event_semaphore()
    assert semaphore is not None

    for _ in range(limit):
        assert not semaphore.locked()
        await semaphore.acquire()

    assert semaphore.locked(), f"a limit of {limit} still had a permit after {limit} were taken"


def test_CONTROL_no_limit_makes_no_semaphore():
    assert _dispatcher(lambda: None, limit=None).get_event_semaphore() is None


def test_a_limit_of_zero_makes_no_semaphore_rather_than_one_nothing_can_acquire():
    """The config refuses 0 (`gt=0`), so this is the dispatcher's own guard, reached with a config
    built around the validation as a hand-made or duck-typed one would be."""
    config = ServiceConfig.model_construct(name="events_svc", max_event_concurrency=0)
    dispatcher = EventDispatcher(
        ServiceRegistry(), config, ExtensionPipeline([]), MagicMock(spec=DeadLetterPublisher)
    )

    assert dispatcher.get_event_semaphore() is None


async def test_a_permit_is_returned_when_the_task_that_held_it_is_done():
    """Under a limit of 1 the second message can only start once the first has given its permit
    back; a permit that is never returned leaves the second callback waiting for good."""
    seen: list[int] = []

    def handler(n: int) -> None:
        seen.append(n)

    dispatcher = _dispatcher(handler, limit=1)
    callback = dispatcher.make_event_callback("evt.a")

    await asyncio.wait_for(callback(_message()), HANDLER_RUNS_WITHIN)
    await _settle()
    await asyncio.wait_for(callback(_message()), HANDLER_RUNS_WITHIN)
    await _settle()

    assert seen == [1, 1]
