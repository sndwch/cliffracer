"""Repository-root pytest configuration and test fixtures covering all test paths."""

import asyncio
import os
import socket

import pytest
import pytest_asyncio

from cliffracer import ServiceConfig


@pytest.fixture(autouse=True)
def _never_bind_a_fixed_port(monkeypatch):
    """Ensure tests bind ephemeral ports for health listeners."""
    from cliffracer.core.health_listener import HealthListener

    monkeypatch.setattr(HealthListener, "_test_port_override", 0)


# Verify tests clean up asyncio background tasks upon completion.
@pytest_asyncio.fixture(autouse=True)
async def _no_leaked_tasks(request):
    yield

    await asyncio.sleep(0)

    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if not pending:
        return

    described = sorted(
        {getattr(t.get_coro(), "__qualname__", None) or str(t.get_coro()) for t in pending}
    )
    for task in pending:
        task.cancel()
    raise AssertionError(
        f"{request.node.nodeid} left {len(pending)} task(s) running: {described}. "
        "Stop the service (await svc.stop()) or cancel the task before the test "
        "ends -- a task that outlives its test can fail a later one."
    )


# Probe broker reachability prior to running integration tests.
NATS_PROBE_TIMEOUT_S = 2.0

# Environment variable specifying the NATS broker URL for integration tests.
TEST_BROKER_URL_ENV = "CLIFFRACER_TEST_NATS_URL"

# The default ServiceConfig.nats_url before runtime overrides.
DEFAULT_BROKER_URL = ServiceConfig.model_fields["nats_url"].default


def configured_broker_url() -> str | None:
    """The URL specified by the operator, or None to retain the default."""
    return os.getenv(TEST_BROKER_URL_ENV) or None


def broker_url() -> str:
    """The address used by the test suite."""
    return ServiceConfig.model_fields["nats_url"].default


def _apply_broker_url(url: str) -> None:
    """Override ServiceConfig default NATS URL for this test process."""
    ServiceConfig.model_fields["nats_url"].default = url
    ServiceConfig.model_rebuild(force=True)


def _nats_endpoint() -> tuple[str, str, int]:
    """Return (url, host, port) of the configured NATS broker."""
    url = broker_url()
    rest = url.split("://", 1)[-1]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    host, _, port = rest.partition(":")
    return url, host or "localhost", int(port or 4222)


def _broker_is_listening() -> tuple[bool, str]:
    """Test TCP connectivity to the configured NATS broker."""
    url, host, port = _nats_endpoint()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(NATS_PROBE_TIMEOUT_S)
    try:
        sock.connect((host, port))
        return True, url
    except OSError:
        return False, url
    finally:
        sock.close()


def pytest_configure(config):
    """Configure pytest with custom markers, and probe for a broker."""
    config.addinivalue_line("markers", "integration: mark test as integration test")
    config.addinivalue_line("markers", "unit: mark test as unit test")
    config.addinivalue_line("markers", "nats_required: mark test as requiring NATS server")

    # BEFORE the probe, so the probe reports on the address the tests will use.
    asked_for = configured_broker_url()
    if asked_for:
        _apply_broker_url(asked_for)
    config._nats_asked_for = asked_for

    reachable, url = _broker_is_listening()
    config._nats_reachable = reachable
    config._nats_url = url
    # Printed, not logged: this has to survive a run that is killed later, and
    # it has to be visible in a backgrounded run's captured output. It is also
    # repeated in the terminal summary, because `| tail` hides this one.
    print(f"\nnats probe: {'broker at ' + url if reachable else 'NO BROKER at ' + url}")

    # An explicitly configured broker must be reachable; fail fast otherwise.
    if asked_for and not reachable:
        raise pytest.UsageError(
            f"{TEST_BROKER_URL_ENV}={asked_for} names a broker that is not "
            f"listening. Start one there, or unset it to fall back to "
            f"{DEFAULT_BROKER_URL} and skip the NATS tests."
        )

    env_url = os.getenv("NATS_URL")
    if env_url:
        # Warn if legacy NATS_URL environment variable is set.
        print(
            f"nats probe: WARNING $NATS_URL={env_url} does not move this suite. "
            f"Use {TEST_BROKER_URL_ENV}; this run dials {url}"
        )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Output broker status and skipped test counts in terminal summary."""
    url = getattr(config, "_nats_url", DEFAULT_BROKER_URL)
    reachable = getattr(config, "_nats_reachable", True)
    skipped = len(getattr(config, "_nats_skipped", []))
    where = "broker at" if reachable else "NO BROKER at"
    line = f"nats: {where} {url}"
    if getattr(config, "_nats_asked_for", None):
        line += f" (from ${TEST_BROKER_URL_ENV})"
    line += f"; {skipped} nats_required test(s) skipped"
    terminalreporter.write_sep("-", line)


def pytest_collection_modifyitems(config, items):
    """Modify test collection to handle markers"""
    config._nats_skipped = []
    # Skip NATS integration tests if NATS_SKIP_INTEGRATION is set
    if os.getenv("NATS_SKIP_INTEGRATION", "false").lower() == "true":
        skip_integration = pytest.mark.skip(reason="NATS integration tests disabled")
        for item in items:
            if "nats_required" in item.keywords:
                item.add_marker(skip_integration)
                config._nats_skipped.append(item.nodeid)
        return

    if not getattr(config, "_nats_reachable", True):
        skip_no_broker = pytest.mark.skip(
            reason=f"no broker at {getattr(config, '_nats_url', DEFAULT_BROKER_URL)}"
        )
        for item in items:
            if "nats_required" in item.keywords:
                item.add_marker(skip_no_broker)
                config._nats_skipped.append(item.nodeid)
