"""Repository-root pytest configuration and test fixtures covering all test paths."""

import asyncio
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from loguru import logger

from cliffracer import ServiceConfig


@pytest.fixture(autouse=True)
def _never_bind_a_fixed_port(monkeypatch):
    """Ensure tests bind ephemeral ports for health listeners."""
    from cliffracer.core.health_listener import HealthListener

    monkeypatch.setattr(HealthListener, "_test_port_override", 0)


#: The ports of the shared development broker: the client port and the monitoring port.
SHARED_BROKER_PORTS = frozenset({4222, 8222})


@pytest.fixture(autouse=True)
def refused_broker_port_dials(request, monkeypatch):
    """A test with no broker of its own dials neither of the shared broker's ports.

    The marker `nats_required` (and the integration and benchmark tiers) is how a test says it uses
    a broker, and it names one by the address in `CLIFFRACER_TEST_NATS_URL`. Any other test that
    connects to port 4222 or 8222 is reading whatever happens to be listening on the host: its result
    can change with the shared broker's state, and a write would land on it. Such a connection is
    refused, as a closed port refuses it, and recorded: the test fails at teardown naming the
    address, so code that swallows the refusal cannot hide it.
    """
    markers = {marker.name for marker in request.node.iter_markers()}
    dialled: list[tuple] = []
    if markers & {"nats_required", "integration", "benchmark"}:
        yield dialled
        return

    def guarded(original):
        def connect(self, address, *args, **kwargs):
            if (
                isinstance(address, tuple)
                and len(address) >= 2
                and address[1] in SHARED_BROKER_PORTS
            ):
                dialled.append(address)
                raise ConnectionRefusedError(
                    f"a test dialled the shared broker's port: {address!r}"
                )
            return original(self, address, *args, **kwargs)

        return connect

    monkeypatch.setattr(socket.socket, "connect", guarded(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(socket.socket.connect_ex))
    yield dialled
    assert not dialled, (
        f"{request.node.nodeid} dialled the shared broker's port: {dialled}. A test with no "
        "broker of its own stubs the connection or names a broker through CLIFFRACER_TEST_NATS_URL "
        "and carries the nats_required marker."
    )


@pytest.fixture
def no_broker_monitor(monkeypatch):
    """The broker's monitoring endpoint answers nothing, whatever is listening on the host."""
    import urllib.error
    import urllib.request

    def refused(*args, **kwargs):
        raise urllib.error.URLError("the monitoring port is not read by this test")

    monkeypatch.setattr(urllib.request, "urlopen", refused)


@pytest.fixture(autouse=True)
def _a_test_leaves_the_process_wide_loguru_extra_as_it_found_it():
    """Put loguru's global ``extra`` back after every test.

    `LoggingConfig.configure` sets `service` there, and a record's own `extra` is merged over it,
    so a test that reads `service` back sees whichever test configured logging last. Loguru
    offers no public way to read the dict, hence the private `_core`.
    """
    before = dict(cast(Any, logger)._core.extra)
    yield
    logger.configure(extra=before)


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
def console_script(name: str) -> str:
    """The path to an installed console script, resolved from the interpreter.

    `shutil.which` searches `PATH`, and the invocation CLAUDE.md documents beside
    `uv run pytest` -- `.venv/bin/python -m pytest` -- does not put `.venv/bin` on
    `PATH`. `uv run` prepends it; running the interpreter directly does not. The
    script is installed either way, so a `PATH` lookup turns a documented
    invocation into five red tests about something else entirely.

    Looking beside `sys.executable` finds the script belonging to the
    interpreter that is running the test, which is the environment whose
    behaviour the test is about. That is a stronger answer than `PATH` even when
    `PATH` would have worked: with two virtualenvs in play, `PATH` can resolve
    to a console script from a different tree than the interpreter under test.

    Raises `AssertionError` naming both places it looked when the script really
    is absent, because "run uv sync" is the wrong remedy for a `PATH` miss and
    the right one for this.
    """
    beside = Path(sys.executable).parent / name
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    found = shutil.which(name)
    if found:
        return found
    # TWO DIFFERENT FAILURES ARRIVE HERE and they have different remedies, so
    # the message separates them instead of naming one.
    #
    # The one this resolver fixes is a PATH miss, and it no longer reaches this
    # point at all. What is left is genuine absence, and that splits again: a
    # virtualenv whose sibling scripts are present but this one is not has had a
    # partial or reverted sync -- `uv run` re-syncs from the lockfile and undoes
    # a manual install, so a script can vanish between two runs with nobody
    # syncing on purpose. A directory with no cliffracer scripts at all is not a
    # cliffracer environment. Reporting the siblings is what tells those apart,
    # and it is the reading a person would go and take by hand.
    bindir = Path(sys.executable).parent
    siblings = sorted(p.name for p in bindir.glob("cliffracer*")) if bindir.is_dir() else []
    if siblings:
        diagnosis = (
            f"that directory holds {siblings}, so it IS a cliffracer environment "
            f"missing this one script -- a partial or reverted sync. Re-run "
            f"`uv sync --all-packages --extra dev` and check it comes back."
        )
    else:
        diagnosis = (
            "that directory holds no cliffracer scripts at all, so it is not a "
            "cliffracer environment. Run the suite through this project's own "
            "virtualenv, or `uv sync --all-packages --extra dev` to build it."
        )
    raise AssertionError(
        f"no console script {name!r}: not beside the running interpreter "
        f"({bindir}) and not on PATH. This is absence, not a PATH miss -- a "
        f"script that is installed but off PATH is found beside the interpreter. "
        f"{diagnosis}"
    )


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

    # A broker nobody named is not dialled, not even to look. Whatever listens on the default
    # address may be a shared one, and a run that found it used it: its `nats_required` tests
    # created and deleted streams on it, and the session cleanup connected to it, for runs that
    # only meant to leave the broker tests out. A broker is used only when `$TEST_BROKER_URL_ENV`
    # names it, which is what CI and the disposable-broker scripts all do.
    if asked_for:
        reachable, url = _broker_is_listening()
    else:
        reachable, url = False, broker_url()
    config._nats_reachable = reachable
    config._nats_url = url
    # Printed, not logged: this has to survive a run that is killed later, and
    # it has to be visible in a backgrounded run's captured output. It is also
    # repeated in the terminal summary, because `| tail` hides this one.
    if asked_for:
        print(f"\nnats probe: {'broker at ' + url if reachable else 'NO BROKER at ' + url}")
    else:
        print(
            f"\nnats probe: NO BROKER NAMED, not dialling {url}. Set ${TEST_BROKER_URL_ENV} to "
            f"run the nats_required tests, which include the integration tier, against a broker "
            f"you chose"
        )

    # An explicitly configured broker must be reachable; fail fast otherwise.
    if asked_for and not reachable:
        raise pytest.UsageError(
            f"{TEST_BROKER_URL_ENV}={asked_for} names a broker that is not "
            f"listening. Start one there, or unset it to skip the NATS tests."
        )

    env_url = os.getenv("NATS_URL")
    if env_url:
        # Warn if legacy NATS_URL environment variable is set.
        print(
            f"nats probe: WARNING $NATS_URL={env_url} does not move this suite. "
            f"Use {TEST_BROKER_URL_ENV}; this run dials {url}"
        )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Report the broker, and what the run actually did with the suite.

    The counts come from the terminalreporter's own stats rather than from
    the list this conftest built, so a test that skips itself inside its body
    is counted like any other skip. Reporting what collection decided would
    print zero for it and read as a full run.
    """
    url = getattr(config, "_nats_url", DEFAULT_BROKER_URL)
    reachable = getattr(config, "_nats_reachable", None)
    asked_for = getattr(config, "_nats_asked_for", None)
    if reachable is True:
        where = "broker at"
    elif reachable is False and not asked_for:
        where = "no broker named, not dialling"
    elif reachable is False:
        where = "NO BROKER at"
    else:
        where = "probe uninitialized at"
    line = f"nats: {where} {url}"
    if asked_for:
        line += f" (from ${TEST_BROKER_URL_ENV})"

    stats = terminalreporter.stats
    ran = sum(len(stats.get(k, [])) for k in ("passed", "failed", "error"))
    skipped = len(stats.get("skipped", []))
    deselected = len(stats.get("deselected", []))
    line += f"; {ran} ran, {skipped} skipped, {deselected} deselected"

    marker_skipped = len(getattr(config, "_nats_skipped", []))
    if marker_skipped:
        line += f" ({marker_skipped} of them held back by the nats_required marker)"

    # A zero above is what a healthy run with a broker prints, and what a run whose skip hook did
    # not run prints too. The marked tests that RAN settle it: with no broker reachable there
    # should be none, and one that did run was not held back, whatever the line above says.
    marked_that_ran = [
        report.nodeid
        for kind in ("passed", "failed", "error")
        for report in stats.get(kind, [])
        if "nats_required" in getattr(report, "keywords", {})
    ]
    line += f"; nats_required: {len(marked_that_ran)} ran"
    terminalreporter.write_sep("-", line)
    if marked_that_ran and reachable is False:
        shown = ", ".join(marked_that_ran[:3]) + (", ..." if len(marked_that_ran) > 3 else "")
        terminalreporter.write_line(
            f"WARNING: {len(marked_that_ran)} nats_required test(s) RAN although no broker was "
            f"reachable at {url}: the marker's skip did not hold them back, so they dialled "
            f"whatever answers there or failed. The collection hook did not run, or did not "
            f"match them. {shown}",
            red=True,
            bold=True,
        )


#: Set by `scripts/check_message_schedules.py`, which starts a broker new enough for the rows it runs.
MESSAGE_SCHEDULES_ENV = "CLIFFRACER_TEST_MESSAGE_SCHEDULES"


def pytest_collection_modifyitems(config, items):
    """Modify test collection to handle markers"""
    config._nats_skipped = []
    # Message schedules need nats-server 2.12+, newer than the suite's broker. Their rows run in
    # the schedule gate, on a broker it starts; anywhere else they are deselected, so they are
    # neither run against a broker that cannot schedule nor counted as skipped.
    if os.getenv(MESSAGE_SCHEDULES_ENV) != "1":
        held = [item for item in items if "message_schedules" in item.keywords]
        if held:
            config.hook.pytest_deselected(items=held)
            items[:] = [item for item in items if "message_schedules" not in item.keywords]
    # Skip NATS integration tests if NATS_SKIP_INTEGRATION is set
    if os.getenv("NATS_SKIP_INTEGRATION", "false").lower() == "true":
        skip_integration = pytest.mark.skip(reason="NATS integration tests disabled")
        for item in items:
            if "nats_required" in item.keywords:
                item.add_marker(skip_integration)
                config._nats_skipped.append(item.nodeid)
        return

    if not getattr(config, "_nats_reachable", True):
        if getattr(config, "_nats_asked_for", None):
            reason = f"no broker at {getattr(config, '_nats_url', DEFAULT_BROKER_URL)}"
        else:
            reason = f"no broker named: set ${TEST_BROKER_URL_ENV} to run it"
        skip_no_broker = pytest.mark.skip(reason=reason)
        for item in items:
            if "nats_required" in item.keywords:
                item.add_marker(skip_no_broker)
                config._nats_skipped.append(item.nodeid)
