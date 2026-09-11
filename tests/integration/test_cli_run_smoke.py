# tests/integration/test_cli_run_smoke.py
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.conftest import broker_url

pytestmark = [pytest.mark.integration, pytest.mark.nats_required, pytest.mark.slow]

MOD = "tests.unit.cli_fixtures.sample_services"

# Pass broker URL explicitly via --nats-url so the subprocess connects to the test broker.


def test_cliffracer_run_starts_and_stops():
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cliffracer.cli",
            "run",
            f"{MOD}:AlphaService",
            "--nats-url",
            broker_url(),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ},
    )
    try:
        time.sleep(5)  # allow connect + handler discovery
        assert proc.poll() is None, "service exited prematurely"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    assert "Running 1 service" in out or "alpha_service" in out
