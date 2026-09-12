"""LoggingExtension: the NATS sink's lifetime, and timing on the hook chain."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from cliffracer_logging import LoggingExtension
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


def _handler_ids() -> set[int]:
    """loguru keeps its handlers in a private dict; the ids are what add()
    returns and remove() takes, so this is the thing that decides."""
    return set(logger._core.handlers)


async def test_no_sink_is_added_unless_asked_for():
    class Svc(CliffracerService):
        logging = LoggingExtension()

    svc = Svc(ServiceConfig(name="quiet"))
    before = _handler_ids()
    await svc.container._setup_extensions()
    await svc.logging.start()
    try:
        assert _handler_ids() == before, "to_nats defaults False; nothing may be added"
        assert svc.logging.health_details() == {"to_nats": False, "streaming": False}
    finally:
        await svc.logging.stop()


async def test_the_sink_is_added_and_then_REMOVED():
    """Verify NATS sink handler is attached on start and detached on stop."""

    class Svc(CliffracerService):
        logging = LoggingExtension(to_nats=True)

    svc = Svc(ServiceConfig(name="loud"))
    svc.nc = AsyncMock()
    before = _handler_ids()

    await svc.container._setup_extensions()
    await svc.logging.start()
    added = _handler_ids() - before
    assert len(added) == 1, f"expected exactly one new loguru handler, got {added}"
    assert svc.logging.health_details() == {"to_nats": True, "streaming": True}

    await svc.logging.stop()
    assert _handler_ids() == before, "stop() must detach the sink it added"
    assert svc.logging.health_details() == {"to_nats": True, "streaming": False}


async def test_start_stop_start_stop_does_not_accumulate_sinks():
    """Verify repeated start/stop cycles do not accumulate sink handlers."""

    class Svc(CliffracerService):
        logging = LoggingExtension(to_nats=True)

    svc = Svc(ServiceConfig(name="cycled"))
    svc.nc = AsyncMock()
    before = _handler_ids()
    await svc.container._setup_extensions()
    for _ in range(3):
        await svc.logging.start()
        await svc.logging.stop()
    assert _handler_ids() == before


async def test_to_nats_without_a_connection_warns_rather_than_raising():
    class Svc(CliffracerService):
        logging = LoggingExtension(to_nats=True)

    svc = Svc(ServiceConfig(name="disconnected"))
    svc.nc = None
    await svc.container._setup_extensions()
    await svc.logging.start()  # must not raise
    try:
        assert svc.logging.health_details()["streaming"] is False
    finally:
        await svc.logging.stop()


async def test_two_services_do_not_share_sink_state():
    """bind() is a shallow copy, so per-instance state is created in setup()."""

    class A(CliffracerService):
        logging = LoggingExtension(to_nats=True)

    class B(CliffracerService):
        logging = LoggingExtension(to_nats=True)

    a, b = A(ServiceConfig(name="a")), B(ServiceConfig(name="b"))
    a.nc, b.nc = AsyncMock(), AsyncMock()
    await a.container._setup_extensions()
    await b.container._setup_extensions()
    assert a.logging is not b.logging
    await a.logging.start()
    try:
        assert a.logging._sink_id is not None
        assert b.logging._sink_id is None, "B must not see A's sink"
    finally:
        await a.logging.stop()


async def test_timing_logs_one_line_per_dispatch_with_the_kind_and_subject():
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG", format="{message}")
    try:

        class Svc(CliffracerService):
            logging = LoggingExtension()

        svc = Svc(ServiceConfig(name="timed"))
        await svc.container._setup_extensions()
        ext = svc.logging
        from cliffracer.core.extension import WorkerContext

        ctx = WorkerContext(
            kind="rpc", subject="timed.echo", headers={}, correlation_id=None, payload={}
        )
        await ext.worker_setup(ctx)
        await asyncio.sleep(0.01)
        await ext.worker_result(ctx, "ok", None)
    finally:
        logger.remove(sink)

    timing = [line for line in lines if "timed.echo" in line]
    assert len(timing) == 1, lines
    assert timing[0].startswith("rpc timed.echo"), timing[0]
    ms = float(timing[0].rsplit(" ", 1)[-1].rstrip("ms\n"))
    assert ms >= 10.0, f"slept 10ms, logged {ms}ms"


async def test_timing_off_logs_nothing_and_leaves_no_key_behind():
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG", format="{message}")
    try:

        class Svc(CliffracerService):
            logging = LoggingExtension(timing=False)

        svc = Svc(ServiceConfig(name="untimed"))
        await svc.container._setup_extensions()
        from cliffracer.core.extension import WorkerContext

        ctx = WorkerContext(
            kind="rpc", subject="untimed.echo", headers={}, correlation_id=None, payload={}
        )
        await svc.logging.worker_setup(ctx)
        assert "_logging_t0" not in ctx.data, "timing=False must not write to the shared scratch"
        await svc.logging.worker_result(ctx, "ok", None)
    finally:
        logger.remove(sink)

    assert not [line for line in lines if "untimed.echo" in line]


def test_structured_mode_actually_writes_parseable_json(tmp_path, monkeypatch):
    """Verify structured mode writes parseable JSON log records."""
    import json
    import os

    from cliffracer_logging import LoggingConfig

    monkeypatch.chdir(tmp_path)
    os.makedirs("logs", exist_ok=True)
    before = set(logger._core.handlers)
    try:
        LoggingConfig.configure(service_name="structured_probe", enable_console=False)
        logger.info("a message that must arrive")
    finally:
        for hid in set(logger._core.handlers) - before:
            logger.remove(hid)

    written = (tmp_path / "logs" / "structured_probe.log").read_text().strip().splitlines()
    assert written, "structured mode wrote no records at all"

    # Verify log record is valid JSON with expected schema.
    record = json.loads(written[-1])
    assert record["record"]["message"] == "a message that must arrive"
    assert record["record"]["extra"]["service"] == "structured_probe"
    assert record["record"]["level"]["name"] == "INFO"

    # Verify log level name matches format expected by add_nats_sink.
    assert record["record"]["level"]["name"].lower() == "info"
