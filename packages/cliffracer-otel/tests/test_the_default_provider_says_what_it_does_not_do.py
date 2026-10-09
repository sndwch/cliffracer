"""A service that supplies no tracer provider is told what the one it gets does not do.

Without a provider `OtelExtension` installs a process-global one whose Resource carries the first
service's name and which has no span processor. OpenTelemetry allows one global provider per
process, so a second service in the process shares it: its spans carry the first service's
`service.name` (the attribute a tracing backend identifies a service by), and nothing was logged.
The global provider is installed once, so each scenario runs in a process of its own.
"""

import json
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

_PROBE = textwrap.dedent(
    """
    import asyncio, json, sys
    from loguru import logger
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from cliffracer import CliffracerService, ServiceConfig
    from cliffracer.core.extension import SharedDependency, WorkerContext
    from cliffracer_otel import OtelExtension

    scenario = sys.argv[1]
    said = []
    logger.remove()
    logger.add(lambda m: said.append(m.record["message"]), level="WARNING")
    exporter = InMemorySpanExporter()

    def provider(name):
        p = TracerProvider(resource=Resource.create({"service.name": name}))
        p.add_span_processor(SimpleSpanProcessor(exporter))
        return p

    def service(name, **extension_args):
        class Svc(CliffracerService):
            otel = OtelExtension(**extension_args)
        return Svc(ServiceConfig(name=name, health_port=0))

    async def main():
        if scenario == "user_global":
            trace.set_tracer_provider(provider("the-operators-name"))
            services = [service("alpha"), service("beta")]
        elif scenario == "default_twice":
            services = [service("alpha"), service("beta")]
        elif scenario == "default_once":
            services = [service("alpha")]
        elif scenario == "default_same_name_twice":
            services = [service("alpha"), service("alpha")]
        elif scenario == "supplied":
            services = [service(n, tracer_provider=SharedDependency(provider(n))) for n in ("alpha", "beta")]
        for svc in services:
            await svc.container._setup_extensions()
        print(json.dumps(said))

    asyncio.run(main())
    """
)


def _run(scenario: str) -> list[str]:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, scenario], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_default_provider_says_it_has_no_span_processor_and_how_to_add_one():
    (said,) = _run("default_once")

    assert "'alpha'" in said
    assert "no span processor" in said
    assert "not exported" in said
    assert "tracer_provider=SharedDependency" in said


def test_a_second_service_sharing_the_default_provider_is_told_whose_name_its_spans_carry():
    first, second = _run("default_twice")

    assert "no span processor" in first
    assert "'beta'" in second and "'alpha'" in second
    assert "service.name='alpha'" in second
    assert "tracer_provider=SharedDependency" in second


def test_the_same_service_name_twice_is_not_told_it_is_misattributed():
    said = _run("default_same_name_twice")

    assert len(said) == 1 and "no span processor" in said[0]


def test_a_provider_the_operator_installed_is_not_second_guessed():
    assert _run("user_global") == []


def test_a_supplied_provider_says_nothing():
    assert _run("supplied") == []
