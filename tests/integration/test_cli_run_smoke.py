# tests/integration/test_cli_run_smoke.py
"""`cliffracer run` as a process: it starts the service, answers on the broker, and stops on SIGTERM.

Each half is read from outside the process. "Starts" is a reply to the service's own `describe`
subject over the broker, which only a service that has connected and subscribed can give; a live
process and a log line printed before the service is ever started are not evidence of it. "Stops"
is the exit status after SIGTERM within a bound, and the same subject going unanswered afterwards.

The service asks for a free health port (`health_port=0` in the `--config` file the command takes), as
ADR-0020 says a process that runs beside others does: the fixture service's default is 8000, and a
second run, or anything else holding 8000, would otherwise fail this by a bind error that surfaces as
a 30-second wait for a reply that never comes.
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time

import nats
import pytest
from nats.errors import NoRespondersError
from nats.errors import TimeoutError as NatsTimeoutError

from cliffracer import ServiceConfig
from cliffracer.core.discovery import HandlerDiscovery
from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required, pytest.mark.slow]


MOD = "tests.unit.cli_fixtures.sample_services"
STARTUP_SECONDS = 30
SHUTDOWN_SECONDS = 10

# Pass broker URL explicitly via --nats-url so the subprocess connects to the test broker.


def _describe_subject() -> str:
    """The service's describe subject under the prefix this test run exports to the subprocess.

    Built inside the test, where the run's subject prefix is set: `ServiceConfig` reads it when it
    is constructed, exactly as the service in the subprocess does.
    """
    return HandlerDiscovery.with_namespace(
        ServiceConfig(name="alpha_service"), "alpha_service.describe"
    )


async def _describe(url: str) -> dict | str:
    """The service's description, or why nothing answered on its describe subject."""
    nc = await nats.connect(url, connect_timeout=5, max_reconnect_attempts=0)
    try:
        reply = await nc.request(_describe_subject(), b"", timeout=1)
    except (NoRespondersError, NatsTimeoutError) as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        await nc.close()
    return json.loads(reply.data)


def _wait_until_it_answers(proc: subprocess.Popen[str], url: str) -> dict:
    deadline = time.monotonic() + STARTUP_SECONDS
    outcome: dict | str = "never asked"
    while time.monotonic() < deadline:
        assert proc.poll() is None, f"the process exited with {proc.returncode} before it answered"
        outcome = asyncio.run(_describe(url))
        if isinstance(outcome, dict):
            return outcome
        time.sleep(0.25)
    raise AssertionError(
        f"nothing answered on {_describe_subject()} within {STARTUP_SECONDS}s: {outcome}"
    )


def _run_and_stop(tmp_path) -> None:
    url = broker_url()
    config = tmp_path / "deploy.yaml"
    config.write_text("global:\n  health_port: 0\n")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cliffracer.cli",
            "run",
            f"{MOD}:AlphaService",
            "--nats-url",
            url,
            "--config",
            str(config),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ},
    )
    out: str | None = None
    try:
        description = _wait_until_it_answers(proc, url)
        assert description["service"] == "alpha_service", description

        proc.send_signal(signal.SIGTERM)
        try:
            returncode = proc.wait(timeout=SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            pytest.fail(f"the process ignored SIGTERM for {SHUTDOWN_SECONDS}s")
        out = proc.communicate()[0]
    except BaseException:
        # Whatever went wrong, the reader of the failure needs what the process said.
        if proc.poll() is None:
            proc.kill()
        print(f"--- its output ---\n{proc.communicate()[0]}")
        raise

    assert returncode == 0, f"exit status {returncode} after SIGTERM:\n{out}"
    assert "Running 1 service" in out, out
    assert "Traceback" not in out, out
    after = asyncio.run(_describe(url))
    assert isinstance(after, str), f"the service still answers after the process exited: {after}"


def test_cliffracer_run_starts_and_stops(tmp_path):
    _run_and_stop(tmp_path)


def test_cliffracer_run_starts_while_the_default_health_port_is_held(tmp_path):
    """Another process on 8000 does not stop the service: it asked for a port of its own."""
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        holder.bind(("127.0.0.1", 8000))
    except OSError:
        holder.close()
        pytest.skip("port 8000 is held by something else, which is the condition this test makes")
    holder.listen()
    try:
        _run_and_stop(tmp_path)
    finally:
        holder.close()
