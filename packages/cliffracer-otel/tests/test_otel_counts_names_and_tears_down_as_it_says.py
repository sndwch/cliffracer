"""What `OtelExtension` counts, names and tears down, read where the rest of the suite does not.

- The health counters start at zero on a service built and not yet started, and restart when the
  extensions are set up again after a stop.
- A `service_name` given to the extension stays its tracer name; setup does not replace it with the
  service's.
- The `tracer` property before setup falls back to a tracer.
- `/health` carries the otel block as soon as setup has run, with no provider anywhere and with one
  already installed, before any traffic. Both run in a fresh process, since installing a provider
  is process-wide.
- A dispatch that started no span (`describe`), and an `after_call` whose `before_call` started
  none, tear down with nothing counted, raised or logged.
- With two dispatches in flight, the `active_spans` gauge reads one once the first has ended, inbound
  and outbound.
- A timer span (a timer has no subject) carries no subject attribute and makes OpenTelemetry warn
  nothing.
"""

import asyncio
import json
import logging
import subprocess
import sys
import textwrap

import pytest
from cliffracer_otel import OtelExtension
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import SharedDependency, WorkerContext

pytestmark = pytest.mark.unit


def _provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _ctx(kind: str, subject: str | None, correlation_id: str | None = None) -> WorkerContext:
    return WorkerContext(
        kind=kind, subject=subject, headers={}, correlation_id=correlation_id, payload={}
    )


async def _service(name: str = "counted-svc", **extension_args) -> CliffracerService:
    class Svc(CliffracerService):
        otel = OtelExtension(**extension_args)

    svc = Svc(ServiceConfig(name=name, health_port=0))
    await svc.container._setup_extensions()
    return svc


def _errors_logged(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if r.levelno >= logging.ERROR]


def _health_after_setup_in_a_fresh_process(prelude: str) -> object:
    code = textwrap.dedent(
        f"""
        import asyncio, json
        {prelude}
        from cliffracer_otel import OtelExtension
        from cliffracer import CliffracerService, ServiceConfig
        class Svc(CliffracerService):
            otel = OtelExtension()
        async def main():
            svc = Svc(ServiceConfig(name="fresh", health_port=0))
            await svc.container._setup_extensions()
            print(json.dumps(svc.otel.health_details()))
        asyncio.run(main())
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-600:]
    return json.loads(out.stdout.strip().splitlines()[-1])


async def test_health_before_the_service_starts_reports_zero_counts():
    provider, _ = _provider()

    class Svc(CliffracerService):
        otel = OtelExtension(tracer=SharedDependency(provider.get_tracer("t")))

    svc = Svc(ServiceConfig(name="not-started", health_port=0))
    details = (await svc.health_check())["otel"]

    assert (details["active_spans"], details["spans_total"], details["errors_total"]) == (0, 0, 0)


async def test_the_counters_restart_when_the_extensions_are_set_up_again():
    provider, _ = _provider()
    svc = await _service(tracer_provider=SharedDependency(provider))

    async def ok():
        return 1

    async def fails():
        raise ValueError("handler failed")

    await svc.container._run_worker(_ctx("rpc", "counted-svc.rpc.a"), ok)
    with pytest.raises(ValueError):
        await svc.container._run_worker(_ctx("rpc", "counted-svc.rpc.b"), fails)
    before = svc.otel.health_details()
    assert (before["spans_total"], before["errors_total"]) == (2, 1)

    await svc.container._stop_extensions()
    await svc.container._setup_extensions()
    after = svc.otel.health_details()

    assert (after["spans_total"], after["errors_total"]) == (0, 0)


async def test_a_service_name_given_to_the_extension_stays_its_tracer_name():
    provider, _ = _provider()
    svc = await _service(service_name="explicit", tracer_provider=SharedDependency(provider))

    assert svc.otel.info_details()["tracer_name"] == "explicit"


def test_the_tracer_before_setup_falls_back_to_a_tracer():
    assert isinstance(OtelExtension().tracer, trace.Tracer)


def test_health_has_the_otel_block_right_after_setup_with_no_provider_anywhere():
    assert _health_after_setup_in_a_fresh_process("") is not None


def test_health_has_the_otel_block_right_after_setup_with_a_provider_already_installed():
    prelude = (
        "from opentelemetry import trace; from opentelemetry.sdk.trace import TracerProvider; "
        "trace.set_tracer_provider(TracerProvider())"
    )
    assert _health_after_setup_in_a_fresh_process(prelude) is not None


async def test_a_dispatch_that_starts_no_span_tears_down_with_nothing_raised_or_logged(caplog):
    provider, _ = _provider()
    svc = await _service(tracer_provider=SharedDependency(provider))
    loguru_errors: list[str] = []
    sink = logger.add(loguru_errors.append, level="ERROR")

    async def describe():
        return "described"

    try:
        with caplog.at_level(logging.ERROR):
            result = await svc.container._run_worker(
                _ctx("describe", "counted-svc.describe"), describe
            )
    finally:
        logger.remove(sink)

    assert result == "described"
    assert _errors_logged(caplog.records) == []
    assert loguru_errors == []


async def test_an_after_call_whose_before_call_started_no_span_counts_and_logs_nothing(caplog):
    provider, _ = _provider()
    svc = await _service(tracer_provider=SharedDependency(provider))

    with caplog.at_level(logging.ERROR):
        await svc.otel.after_call(_ctx("call_rpc", "other.rpc.x"), None, None)

    details = svc.otel.health_details()
    assert (details["spans_total"], details["active_spans"]) == (0, 0)
    assert _errors_logged(caplog.records) == []


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
async def test_the_gauge_reads_one_once_the_first_of_two_in_flight_has_ended(direction):
    provider, _ = _provider()
    svc = await _service(tracer_provider=SharedDependency(provider))
    first_ended = asyncio.Event()
    seen: list[int] = []

    def run(name: str, body):
        if direction == "inbound":
            return svc.container._run_worker(_ctx("rpc", f"counted-svc.rpc.{name}"), body)
        return svc.container._run_send_hooks(_ctx("call_rpc", f"other.rpc.{name}"), body)

    async def first():
        return 1

    async def second():
        await first_ended.wait()
        seen.append(svc.otel.health_details()["active_spans"])

    async def run_first():
        # Lets the second start its span first, so both are in flight when the first ends.
        await asyncio.sleep(0)
        await run("first", first)
        first_ended.set()

    await asyncio.wait_for(asyncio.gather(run("second", second), run_first()), timeout=10)

    assert seen == [1]
    assert svc.otel.health_details()["active_spans"] == 0


async def test_a_timer_span_carries_no_subject_and_warns_nothing(caplog):
    provider, exporter = _provider()
    svc = await _service(tracer_provider=SharedDependency(provider))

    async def tick():
        return None

    with caplog.at_level(logging.WARNING):
        await svc.container._run_worker(_ctx("timer", None, "corr-1"), tick)

    (span,) = exporter.get_finished_spans()
    assert "cliffracer.subject" not in span.attributes, dict(span.attributes)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
