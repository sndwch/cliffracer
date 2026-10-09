"""The suite's broker configuration reaches tests under packages/, in every run.

The probe below asserts the reach but can only run when `$CLIFFRACER_TEST_NATS_URL` is set,
which only CI sets, so a developer's run skipped the one assertion this file is named for. The
second test runs the probe itself in a child pytest with the variable set to a socket that is
listening on a free port (the suite refuses a named broker that is not), so the reach is checked
whether or not the outer run named a broker.
"""

import os
import socket
import subprocess
import sys

import pytest

from cliffracer import ServiceConfig

pytestmark = pytest.mark.unit

BROKER_ENV = "CLIFFRACER_TEST_NATS_URL"
PROBE = "test_a_service_built_under_packages_dials_the_configured_broker"


def test_a_service_built_under_packages_dials_the_configured_broker():
    asked = os.getenv(BROKER_ENV)
    if not asked:
        pytest.skip(f"${BROKER_ENV} is not set; test_the_reach_is_checked_in_a_child_run does it")

    assert ServiceConfig(name="reach-probe").nats_url == asked


def test_the_reach_is_checked_in_a_child_run_whatever_the_outer_run_named():
    with socket.socket() as listener:
        listener.bind((socket.gethostbyname("localhost"), 0))
        listener.listen()
        host, port = listener.getsockname()[:2]
        named = f"nats://{host}:{port}"
        env = {**os.environ, BROKER_ENV: named}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"{__file__}::{PROBE}",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout and "1 skipped" not in result.stdout, result.stdout


def test_CONTROL_an_explicit_url_still_wins():
    """Verify explicit nats_url overrides environment configuration."""
    named = ServiceConfig(name="reach-probe", nats_url="nats://elsewhere:4222")

    assert named.nats_url == "nats://elsewhere:4222"
    assert ServiceConfig(name="reach-probe").nats_url != "nats://elsewhere:4222"
