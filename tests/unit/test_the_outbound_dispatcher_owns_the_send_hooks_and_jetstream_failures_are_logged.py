"""Cleanups in the dispatch layer: the outbound collaborator is the live path, and two
`jetstream.py` failure paths say so instead of staying silent.

- `MessageDispatcher._run_send_hooks` went straight to the pipeline, past the collaborator whose
  job it is, so `OutboundDispatcher.run_send_hooks` was reachable from nowhere and two of its
  constructor arguments were never read. The facade now routes through it, and the constructor
  takes only what it uses.
- `pull_once` recognised a timeout by `type(exc).__name__ == "TimeoutError"` as well as by class,
  a dead branch (nats' error subclasses the builtin) that also matched any exception that merely
  shared the name; it matches the class alone.
- A pull loop that could not unsubscribe on exit said nothing, and `report_consumer_drift`
  logged "could not read consumer info" at DEBUG when it had no pattern, which hid that the
  server's tuning was unchecked. Both warn.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nats.errors
import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.dispatch.outbound import OutboundDispatcher

pytestmark = pytest.mark.unit


def _service() -> CliffracerService:
    return CliffracerService(ServiceConfig(name="svc", health_port=0))


async def test_the_facades_send_hooks_run_through_the_outbound_collaborator():
    svc = _service()
    outbound = svc.container.dispatcher.outbound
    seen: list[str] = []

    async def through_outbound(ctx, send):
        seen.append("outbound")
        return await send()

    outbound.run_send_hooks = through_outbound  # type: ignore[method-assign]
    ctx = svc.container.dispatcher._send_context("rpc", "x.rpc.y", {}, "cid")

    async def send() -> str:
        return "sent"

    result = await svc.container.dispatcher._run_send_hooks(ctx, send)

    assert result == "sent" and seen == ["outbound"]


def test_the_outbound_dispatcher_is_constructed_from_what_it_reads():
    assert list(inspect.signature(OutboundDispatcher).parameters) == ["config", "pipeline"]


class _Fetching:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def fetch(self, *args, **kwargs):
        raise self._error


async def test_a_nats_timeout_from_a_fetch_is_an_empty_batch():
    jetstream = _service().container.dispatcher.jetstream

    assert await jetstream.pull_once(_Fetching(nats.errors.TimeoutError())) == 0


async def test_an_exception_that_merely_shares_the_name_of_a_timeout_is_not_swallowed():
    class TimeoutError(Exception):  # noqa: A001 - the point: same name, not a timeout
        pass

    jetstream = _service().container.dispatcher.jetstream

    with pytest.raises(TimeoutError):
        await jetstream.pull_once(_Fetching(TimeoutError("not a timeout")))


def _sink() -> tuple[list[str], int]:
    lines: list[str] = []
    return lines, logger.add(lambda message: lines.append(str(message)), level="WARNING")


async def test_a_pull_loop_that_cannot_unsubscribe_on_exit_says_so():
    svc = _service()
    jetstream = svc.container.dispatcher.jetstream
    svc.container.nc = SimpleNamespace(is_draining=False, is_closed=False)
    sub = AsyncMock()
    sub.unsubscribe.side_effect = RuntimeError("the server did not answer")
    lines, sink = _sink()
    try:
        await jetstream.pull_loop(sub, "worker", is_running_fn=lambda: False)
    finally:
        logger.remove(sink)

    assert any("worker" in line and "the server did not answer" in line for line in lines), lines


async def test_CONTROL_a_loop_that_exits_while_the_connection_drains_does_not_try_or_warn():
    svc = _service()
    jetstream = svc.container.dispatcher.jetstream
    svc.container.nc = SimpleNamespace(is_draining=True, is_closed=False)
    sub = AsyncMock()
    lines, sink = _sink()
    try:
        await jetstream.pull_loop(sub, "worker", is_running_fn=lambda: False)
    finally:
        logger.remove(sink)

    assert sub.unsubscribe.await_count == 0 and lines == []


async def test_an_unreadable_consumer_is_warned_about_even_without_a_pattern():
    jetstream = _service().container.dispatcher.jetstream
    sub = AsyncMock()
    sub.consumer_info.side_effect = RuntimeError("no such consumer")
    lines, sink = _sink()
    try:
        await jetstream.report_consumer_drift(sub, "worker")
    finally:
        logger.remove(sink)

    assert any("worker" in line and "no such consumer" in line for line in lines), lines
