"""A bare `RpcDispatcher` starts, bounds and calls handlers as it is configured to.

Driven without a container, so nothing else spawns, bounds or fills in for it:

- WHO STARTS A REQUEST: the task spawner it was given, under the name the arm gives the task; and
  with none, `asyncio.create_task`. A dispatcher built with no spawner is a supported shape.
- HOW MANY RUN AT ONCE: a limit of N is a semaphore of N (so a limit of ONE is a bound), a limit
  of zero or less is no semaphore, the async limit falls back to the sync one only when unset, and
  a permit is handed back when the task is done, so the next request is let in.
- WHAT THE HANDLER IS CALLED WITH when no extension has validated the payload: the payload itself
  when it is an object, nothing when it is anything else, and the correlation id when the handler
  asks for it. An async RPC handler written as a plain `def` is called, not awaited.
- WHAT IS KEPT: a config that cannot be fingerprinted is described afresh every time and its answer
  is held nowhere.

Limits are written as literals. A limit of zero cannot come from a validated `ServiceConfig`
(`gt=0`), so those cases hand the dispatcher a config stand-in; the dispatcher reads only the
three fields it is given.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from cliffracer import ServiceConfig
from cliffracer.core.dispatch import ExtensionPipeline, RpcDispatcher
from cliffracer.core.extension import Extension
from cliffracer.core.registry import ServiceRegistry
from cliffracer.core.typed_rpc import build_handler_spec
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit

JSON = {"Content-Type": "application/json"}


class RecordingLogger:
    """Collects what the dispatcher logs, by level."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def __getattr__(self, level: str):
        def log(message: str, *args: Any, **kwargs: Any) -> None:
            self.lines.append((level, message))

        return log

    def at(self, level: str) -> list[str]:
        return [m for lv, m in self.lines if lv == level]


class Stamp(Extension):
    """Gives every dispatch a correlation id, as the correlation extension does."""

    async def worker_setup(self, ctx) -> None:
        ctx.correlation_id = "cid-1"


class Watch(Extension):
    """Records what `worker_result` is told."""

    def __init__(self) -> None:
        self.results: list[tuple[Any, BaseException | None]] = []

    async def worker_result(self, ctx, result, exc) -> None:
        self.results.append((result, exc))


def _dispatcher(
    *handlers: Any,
    config: Any = None,
    spawner: Any = None,
    extensions: list[Extension] | None = None,
    logger: Any = None,
) -> RpcDispatcher:
    registry = ServiceRegistry()
    for fn in handlers:
        registry.rpc_handlers[fn.__name__] = fn
        registry.rpc_specs[fn.__name__] = build_handler_spec(fn.__name__, fn, owner=object)
    config = config or ServiceConfig(name="s", health_listener=False)
    return RpcDispatcher(
        registry, config, ExtensionPipeline(extensions or []), spawner, logger or RecordingLogger()
    )


def _msg(name: str, body: Any = None, *, reply: str = "_INBOX.c") -> MockMessage:
    data = b"" if body is None else json.dumps(body).encode()
    return MockMessage(f"s.rpc.{name}", data, headers=dict(JSON), reply=reply)


async def _until(predicate, what: str, *, seconds: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


def _handler_that_answers():
    def answer_me() -> int:
        return 3

    return answer_me


# --- who starts a request -----------------------------------------------------------------------


async def test_with_no_task_spawner_a_request_runs_as_an_asyncio_task():
    dispatcher = _dispatcher(_handler_that_answers())
    msg = _msg("answer_me")

    await dispatcher.on_rpc_request(msg)
    await _until(lambda: msg.responded_data is not None, "the reply")

    assert json.loads(msg.responded_data)["result"] == 3


async def test_with_no_task_spawner_an_async_request_runs_as_an_asyncio_task():
    calls: list[int] = []

    def note(value: int) -> None:
        calls.append(value)

    dispatcher = _dispatcher(note)

    await dispatcher.on_async_request(_msg("note", {"value": 4}, reply=""))
    await _until(lambda: calls, "the handler")

    assert calls == [4]


async def test_with_no_task_spawner_a_describe_request_is_answered():
    dispatcher = _dispatcher(_handler_that_answers())
    msg = MockMessage("s.describe", b"", reply="_INBOX.c")

    await dispatcher.on_describe_request(msg)
    await _until(lambda: msg.responded_data is not None, "the describe reply")

    assert json.loads(msg.responded_data)["service"] == "s"


@pytest.mark.parametrize(
    ("arm", "limits", "expected_name"),
    [
        ("rpc", {}, "rpc_request"),
        ("rpc", {"max_rpc_concurrency": 1}, "rpc_bounded_request"),
        ("async", {}, "async_rpc_request"),
        ("async", {"max_async_rpc_concurrency": 1}, "async_rpc_bounded_request"),
        ("describe", {}, "describe_request"),
    ],
)
async def test_the_task_spawner_it_was_given_starts_the_request_under_the_arms_name(
    arm, limits, expected_name
):
    spawned: list[str | None] = []
    calls: list[int] = []

    def spawner(coro, name=None):
        spawned.append(name)
        return asyncio.create_task(coro, name=name)

    def note(value: int) -> None:
        calls.append(value)

    config = ServiceConfig(name="s", health_listener=False, **limits)
    dispatcher = _dispatcher(note, _handler_that_answers(), config=config, spawner=spawner)

    if arm == "rpc":
        msg = _msg("answer_me")
        await dispatcher.on_rpc_request(msg)
        await _until(lambda: msg.responded_data is not None, "the reply")
    elif arm == "async":
        await dispatcher.on_async_request(_msg("note", {"value": 1}, reply=""))
        await _until(lambda: calls, "the handler")
    else:
        msg = MockMessage("s.describe", b"", reply="_INBOX.c")
        await dispatcher.on_describe_request(msg)
        await _until(lambda: msg.responded_data is not None, "the reply")

    assert spawned == [expected_name]


# --- how many run at once -----------------------------------------------------------------------


async def test_a_permit_is_handed_back_when_the_request_is_done():
    """With room for ONE, the second request is let in only because the first returned its permit."""
    dispatcher = _dispatcher(
        _handler_that_answers(), config=ServiceConfig(name="s", max_rpc_concurrency=1)
    )
    first, second = _msg("answer_me"), _msg("answer_me")

    await asyncio.wait_for(dispatcher.on_rpc_request(first), timeout=3)
    await asyncio.wait_for(dispatcher.on_rpc_request(second), timeout=3)
    await _until(lambda: second.responded_data is not None, "the second reply")

    assert first.responded_data is not None


async def test_an_async_permit_is_handed_back_when_the_request_is_done():
    calls: list[int] = []

    def note(value: int) -> None:
        calls.append(value)

    dispatcher = _dispatcher(note, config=ServiceConfig(name="s", max_async_rpc_concurrency=1))

    await asyncio.wait_for(dispatcher.on_async_request(_msg("note", {"value": 1}, reply="")), 3)
    await asyncio.wait_for(dispatcher.on_async_request(_msg("note", {"value": 2}, reply="")), 3)
    await _until(lambda: len(calls) == 2, "both handlers")

    assert sorted(calls) == [1, 2]


async def test_a_limit_of_one_is_a_semaphore_of_one():
    dispatcher = _dispatcher(config=ServiceConfig(name="s", max_async_rpc_concurrency=1))
    sem = dispatcher.get_async_rpc_semaphore()

    assert sem is not None
    await sem.acquire()
    assert sem.locked()


async def test_the_async_limit_falls_back_to_the_sync_one_only_when_unset():
    fallback = _dispatcher(config=ServiceConfig(name="s", max_rpc_concurrency=2))
    own = _dispatcher(
        config=ServiceConfig(name="s", max_rpc_concurrency=2, max_async_rpc_concurrency=1)
    )

    fallen_back = fallback.get_async_rpc_semaphore()
    its_own = own.get_async_rpc_semaphore()

    assert fallen_back is not None and its_own is not None
    await fallen_back.acquire()
    assert not fallen_back.locked(), "a limit of two is locked by one permit"
    await its_own.acquire()
    assert its_own.locked(), "the async limit of one is what applies"


async def test_no_limit_is_no_semaphore():
    dispatcher = _dispatcher(config=ServiceConfig(name="s"))

    assert dispatcher.get_rpc_semaphore() is None
    assert dispatcher.get_async_rpc_semaphore() is None


@pytest.mark.parametrize(
    ("rpc_limit", "async_limit"),
    [(0, 0), (0, None), (None, 0), (-1, None)],
    ids=["both_zero", "sync_zero_async_unset", "async_zero_only", "negative_falls_back"],
)
async def test_a_limit_of_zero_or_less_is_no_bound_not_a_semaphore_nobody_can_enter(
    rpc_limit, async_limit
):
    """A semaphore of zero would hold every request forever; a validated config cannot say it."""
    config = SimpleNamespace(
        name="s", max_rpc_concurrency=rpc_limit, max_async_rpc_concurrency=async_limit
    )
    dispatcher = _dispatcher(config=config)

    assert dispatcher.get_async_rpc_semaphore() is None
    if rpc_limit is not None:
        assert dispatcher.get_rpc_semaphore() is None


# --- what the handler is called with, with no validating extension ------------------------------


async def test_an_object_payload_is_the_handlers_arguments():
    calls: list[int] = []

    def note(value: int) -> None:
        calls.append(value)

    dispatcher = _dispatcher(note)

    await dispatcher.handle_async_request(_msg("note", {"value": 5}, reply=""))

    assert calls == [5]


async def test_a_payload_that_is_not_an_object_gives_the_async_handler_no_arguments():
    calls: list[str] = []

    def nothing() -> None:
        calls.append("called")

    dispatcher = _dispatcher(nothing)

    await dispatcher.handle_async_request(_msg("nothing", [1, 2], reply=""))

    assert calls == ["called"]


async def test_a_payload_that_is_not_an_object_gives_the_sync_handler_no_arguments():
    dispatcher = _dispatcher(_handler_that_answers())
    msg = _msg("answer_me", [1, 2])

    await dispatcher.handle_rpc_request(msg)

    assert json.loads(msg.responded_data)["result"] == 3, msg.responded_data


async def test_an_async_handler_that_asks_for_the_correlation_id_is_given_it():
    seen: list[tuple[int, str | None]] = []

    def tag(value: int, correlation_id: str | None = None) -> None:
        seen.append((value, correlation_id))

    dispatcher = _dispatcher(tag, extensions=[Stamp()])

    await dispatcher.handle_async_request(_msg("tag", {"value": 1}, reply=""))

    assert seen == [(1, "cid-1")]


async def test_an_async_rpc_handler_written_as_a_plain_def_is_called_not_awaited():
    seen: list[int] = []

    def note(value: int) -> str:
        seen.append(value)
        return "done"

    watch = Watch()
    logger = RecordingLogger()
    dispatcher = _dispatcher(note, extensions=[watch], logger=logger)

    await dispatcher.handle_async_request(_msg("note", {"value": 8}, reply=""))

    assert seen == [8]
    assert watch.results == [("done", None)], watch.results
    assert logger.at("error") == []


async def test_CONTROL_an_async_def_handler_is_awaited_on_the_same_path():
    seen: list[int] = []

    async def note(value: int) -> str:
        seen.append(value)
        return "done"

    watch = Watch()
    dispatcher = _dispatcher(note, extensions=[watch])

    await dispatcher.handle_async_request(_msg("note", {"value": 8}, reply=""))

    assert watch.results == [("done", None)]


# --- what is kept -------------------------------------------------------------------------------


async def test_a_config_that_cannot_be_fingerprinted_is_described_afresh_and_held_nowhere(
    monkeypatch,
):
    monkeypatch.setattr(
        ServiceConfig,
        "model_dump_json",
        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("cannot fingerprint")),
    )
    dispatcher = _dispatcher(config=ServiceConfig(name="s", health_listener=False))
    msg = MockMessage("s.describe", b"", reply="_INBOX.c")

    await dispatcher.handle_describe_request(msg)

    assert json.loads(msg.responded_data)["service"] == "s"
    assert dispatcher._describe._built is None
