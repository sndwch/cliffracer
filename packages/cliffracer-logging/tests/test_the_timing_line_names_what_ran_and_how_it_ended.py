"""A timing line says which handler ran and whether it raised.

The line was `<kind> <subject> <ms>ms`. A timer dispatch has no subject, so it read `timer None
10.2ms`, naming neither the timer nor the service; and a handler that raised logged the same line
as one that returned, so slow-and-failing could not be told from slow-and-fine. A timer's method
name is now on the dispatch context, the line names it, and it ends with `ok` or `failed=<Error>`.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from cliffracer_logging import LoggingExtension
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import WorkerContext
from cliffracer.core.timer import Timer

pytestmark = pytest.mark.unit


@pytest.fixture
def lines():
    captured: list[str] = []
    sink = logger.add(captured.append, level="DEBUG", format="{message}")
    yield captured
    logger.remove(sink)


class Svc(CliffracerService):
    logging = LoggingExtension()

    async def sweep(self) -> None:
        await asyncio.sleep(0.01)

    async def boom(self) -> None:
        raise RuntimeError("the timer's handler failed")


async def _fire(svc: Svc, method: str) -> None:
    """One real timer firing: the method runs inside the service's hook chain."""
    timer = Timer(interval=60.0)
    timer.method_name = method
    timer.service_instance = svc
    await timer._execute_method()


def _timing(lines: list[str], what: str) -> list[str]:
    return [line.strip() for line in lines if line.startswith("timer ") and what in line]


async def test_a_timer_firing_is_named_by_its_method_and_ends_ok(lines):
    svc = Svc(ServiceConfig(name="timed"))
    await svc.container._setup_extensions()

    await _fire(svc, "sweep")

    (line,) = _timing(lines, "sweep")
    kind, name, duration, outcome = line.split()
    assert (kind, name, outcome) == ("timer", "sweep", "ok"), line
    assert float(duration.removesuffix("ms")) >= 10.0, line
    assert "None" not in line


async def test_a_timer_that_raises_logs_the_error_type_instead_of_ok(lines):
    svc = Svc(ServiceConfig(name="timed"))
    await svc.container._setup_extensions()

    await _fire(svc, "boom")

    (line,) = _timing(lines, "boom")
    assert line.split()[-1] == "failed=RuntimeError", line


async def test_an_rpc_that_raises_is_told_from_one_that_returned(lines):
    ext = LoggingExtension()
    ok = WorkerContext(kind="rpc", subject="svc.rpc.a", headers={}, correlation_id=None, payload={})
    bad = WorkerContext(
        kind="rpc", subject="svc.rpc.b", headers={}, correlation_id=None, payload={}
    )
    for ctx in (ok, bad):
        await ext.worker_setup(ctx)

    await ext.worker_result(ok, "result", None)
    await ext.worker_result(bad, None, ValueError("nope"))

    by_subject = {line.split()[1]: line.split()[-1] for line in lines if line.startswith("rpc ")}
    assert by_subject == {"svc.rpc.a": "ok", "svc.rpc.b": "failed=ValueError"}


async def test_a_subject_still_wins_over_a_handler_name(lines):
    ext = LoggingExtension()
    ctx = WorkerContext(
        kind="event", subject="orders.created", headers={}, correlation_id=None, payload={}
    )
    ctx.data["handler_name"] = "on_order_created"
    await ext.worker_setup(ctx)

    await ext.worker_result(ctx, None, None)

    assert [line.split()[1] for line in lines if line.startswith("event ")] == ["orders.created"]


async def test_re_running_setup_does_not_orphan_a_live_sink():
    """A restart re-runs every extension's `setup`; the sink a running extension holds is not
    state `setup` may reset, or `stop()` would no longer remove it."""

    class Streaming(CliffracerService):
        logging = LoggingExtension(to_nats=True)

    svc = Streaming(ServiceConfig(name="streaming"))
    svc.nc = AsyncMock()
    await svc.container._setup_extensions()
    await svc.logging.start()
    sink_id = svc.logging._sink_id
    try:
        assert sink_id is not None

        svc.container._extensions_set_up = False  # what a stop followed by a start does
        await svc.container._setup_extensions()

        assert svc.logging._sink_id == sink_id
        assert svc.logging.health_details()["streaming"] is True
    finally:
        await svc.logging.stop()
