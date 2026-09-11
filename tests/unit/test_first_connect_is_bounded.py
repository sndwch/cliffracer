"""Tests ensuring initial NATS connection attempts are bounded by connect_timeout."""

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import nats
import pytest
from nats.errors import Error as NatsError

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit

# Port 1 is privileged: nothing of ours can be listening there by accident.
# Same address the broker-url guard uses for the same reason.
DEAD_URL = "nats://127.0.0.1:1"
REPO = Path(__file__).resolve().parents[2]


class _Recorder:
    """Stands in for `service.logger`. The container reads it late, on purpose,
    which is what makes this substitution work (see `Container.logger`)."""

    def __init__(self):
        self.errors: list[str] = []

    def error(self, message):
        self.errors.append(str(message))

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _service(**overrides):
    cfg = ServiceConfig(name="brokerless", health_port=0, nats_url=DEAD_URL, **overrides)
    svc = CliffracerService(cfg)
    svc.logger = _Recorder()
    return svc


# --- the connect returns instead of hanging ----------------------------------


async def test_a_brokerless_connect_raises_instead_of_hanging():
    svc = _service(exit_on_closed=False, connect_timeout=1.0)
    with pytest.raises(NatsError):
        await asyncio.wait_for(svc.container.connect(), timeout=10)


async def test_the_error_names_the_service_the_reason_and_redacts_the_password():
    """Ensure the connection error message includes service name, reason, and redacted credentials."""
    svc = ServiceConfig(
        name="named_svc",
        health_port=0,
        nats_url="nats://someone:hunter2@127.0.0.1:1",
        exit_on_closed=False,
        connect_timeout=1.0,
    )
    service = CliffracerService(svc)
    service.logger = _Recorder()
    with pytest.raises(NatsError):
        await asyncio.wait_for(service.container.connect(), timeout=10)

    said = "\n".join(service.logger.errors)
    assert "named_svc" in said, said
    assert "127.0.0.1:1" in said, said
    assert "connect_timeout=1.0" in said, said
    assert "hunter2" not in said, "the password reached the log"


async def test_CONTROL_none_restores_the_unbounded_behaviour():
    """The control that says the timeout is what does the work.

    Without it, a `connect` that failed for some unrelated reason would satisfy
    every test above and this change would look effective while doing nothing.
    """
    svc = _service(exit_on_closed=False, connect_timeout=None)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(svc.container.connect(), timeout=3)


# --- the connect failure raises cleanly instead of hard os._exit(75) ---------


def test_the_process_raises_nats_error_and_does_not_exit_75():
    """Verify failed initial connection raises NatsError cleanly without hard os._exit(75)."""
    script = (
        "import asyncio\n"
        "from cliffracer import CliffracerService, ServiceConfig\n"
        "from nats.errors import Error as NatsError\n"
        "async def main():\n"
        "    svc = CliffracerService(ServiceConfig(name='rc_svc', health_port=0,\n"
        f"        nats_url='{DEAD_URL}', connect_timeout=1.0))\n"
        "    try:\n"
        "        await svc.container.connect()\n"
        "    except NatsError:\n"
        "        print('CAUGHT_NATS_ERROR')\n"
        "asyncio.run(main())\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, f"script failed: {done.stderr[-2000:]}"
    assert "CAUGHT_NATS_ERROR" in done.stdout
    assert "rc_svc" in done.stderr, done.stderr[-2000:]


# --- what nats-py actually does with the budget, pinned ----------------------


@pytest.mark.parametrize("attempts", [-1, 0])
async def test_both_minus_one_and_zero_retry_the_first_connect_forever(attempts, caplog):
    """Verify nats.connect with attempts=-1 and 0 retries initial connect until timeout."""
    caplog.set_level(logging.CRITICAL, logger="nats.aio.client")
    errors = []

    async def _error_cb(err):
        errors.append(err)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            nats.connect(
                DEAD_URL,
                max_reconnect_attempts=attempts,
                reconnect_time_wait=0.05,
                error_cb=_error_cb,
            ),
            timeout=0.5,
        )
    assert len(errors) >= 2, f"expected retries (>= 2), observed {len(errors)}"


async def test_CONTROL_a_finite_budget_does_return(caplog):
    """The other half: without it, the two above would pass against a nats-py
    that never returned for any value, and say nothing about `0`."""
    caplog.set_level(logging.CRITICAL, logger="nats.aio.client")
    errors = []

    async def _error_cb(err):
        errors.append(err)

    with pytest.raises(NatsError):
        await asyncio.wait_for(
            nats.connect(
                DEAD_URL,
                max_reconnect_attempts=1,
                reconnect_time_wait=0.01,
                error_cb=_error_cb,
            ),
            timeout=10,
        )
    assert len(errors) >= 1


# --- a service that CAN connect is untouched ---------------------------------


@pytest.mark.nats_required
async def test_CONTROL_a_reachable_broker_is_unaffected():
    """The deadline must not be making everything fail."""
    svc = CliffracerService(ServiceConfig(name="reachable", health_port=0))
    await svc.start()
    try:
        assert svc.nc is not None and not svc.nc.is_closed
    finally:
        await svc.stop()
