"""The warnings an `OtelExtension` writes for a service are bound to that service.

A NATS log sink publishes only the records bound to its service. The extension wrote its two
warnings (it installed a process-wide tracer provider with no span processor; it shares the one
installed for another service) through the bare loguru logger, so neither reached a
`logs.<service>.<level>` stream. They are written through the logger the extension base binds to
its service. The global provider is installed once per process, so the scenario runs in a process
of its own: `alpha` installs it, `beta` shares it, each with a sink filtered to its own service.
"""

import json
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

_PROBE = textwrap.dedent(
    """
    import asyncio, json
    from cliffracer_logging import LoggingConfig
    from loguru import logger
    from cliffracer import CliffracerService, ServiceConfig
    from cliffracer_otel import OtelExtension

    class Recorder:
        def __init__(self):
            self.messages = []
        async def publish(self, subject, payload):
            self.messages.append(payload.decode())

    logger.remove()

    def service(name):
        class Svc(CliffracerService):
            otel = OtelExtension()
        return Svc(ServiceConfig(name=name, subject_prefix=None, health_port=0))

    async def main():
        streams = {"alpha": Recorder(), "beta": Recorder()}
        for name, nc in streams.items():
            config = ServiceConfig(name=name, subject_prefix=None, health_port=0)
            LoggingConfig.add_nats_sink(name, nc, config=config, log_level="WARNING")
        for name in ("alpha", "beta"):
            await service(name).container._setup_extensions()
        logger.complete()
        await asyncio.sleep(0.3)
        logger.complete()
        print(json.dumps({n: s.messages for n, s in streams.items()}))

    asyncio.run(main())
    """
)


def _streams() -> dict[str, list[str]]:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_each_warning_is_in_the_stream_of_the_service_it_was_written_for():
    streams = _streams()

    # alpha installed the provider, so its stream has the "no span processor" line; beta shared
    # alpha's provider, so its stream has the line naming alpha. Neither stream has the other's.
    assert [m for m in streams["alpha"] if "no span processor" in m], streams
    assert not [m for m in streams["alpha"] if "shares the tracer provider" in m], streams
    assert [m for m in streams["beta"] if "shares the tracer provider" in m], streams
    assert not [m for m in streams["beta"] if "no span processor" in m], streams
