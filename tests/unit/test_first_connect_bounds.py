"""Tests ensuring initial NATS connection attempts are bounded by connect_timeout."""

import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.fixture
def recorded_bounds(monkeypatch):
    """Every timeout handed to `asyncio.wait_for` while the fixture is active."""
    seen: list[float | None] = []
    real = asyncio.wait_for

    async def recording(aw, timeout):
        seen.append(timeout)
        return await real(aw, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", recording)
    return seen


# --- the deadline itself ------------------------------------------------------
#
# The tests further down show that a dead broker makes `connect()` raise within a
# generous outer bound, and that the error message names `connect_timeout=1.0`. Neither
# reads the deadline `connect()` applied: any hard-coded bound under that outer one
# satisfies the first, and the second reads a string interpolated from the config, which
# stays right whatever `wait_for` was given. These read what `wait_for` received, for
# three different values so that no single constant can satisfy them all.


def _dialling_client(connect):
    """The client `cliffracer.core.dial` builds, whose `connect` is `connect`: the dial helper's own
    bound and cleanup run, and only nats-py's side of it is replaced."""
    return patch(
        "cliffracer.core.dial.nats.NATS",
        return_value=MagicMock(connect=connect, close=AsyncMock()),
    )


@pytest.mark.parametrize("bound", [0.05, 0.2, 7.5])
async def test_the_configured_connect_timeout_is_the_deadline_the_dial_is_given(
    bound, recorded_bounds
):
    svc = _service(exit_on_closed=False, connect_timeout=bound)

    with _dialling_client(AsyncMock()):
        await svc.container.connect()

    assert recorded_bounds == [bound], recorded_bounds


async def test_a_dial_that_outlives_the_bound_is_cut_off_at_it_and_says_so():
    async def never_answers(*args, **kwargs):
        await asyncio.sleep(3600)

    svc = _service(exit_on_closed=False, connect_timeout=0.05)

    with _dialling_client(never_answers):
        # The outer bound is two orders of magnitude above the bound under test: it turns
        # "the deadline is not applied" into a failure, not a hung suite.
        with pytest.raises(NatsError, match=r"connect_timeout=0\.05"):
            await asyncio.wait_for(svc.container.connect(), timeout=5.0)


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
    assert "connect_timeout=1.0" in said, said  # the rendered config, not the applied deadline
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


# --- start() and run(), not only container.connect() ------------------------
#
# The decision is about startup: if the broker is unreachable, `start()` raises `NatsError`
# once `connect_timeout` expires. Every test above calls `container.connect()`, which is one
# step of `start()`; a change that caught the failure in `start()` and carried on kept them all
# green while `start()` returned or raised from a later step with a different exception.


async def test_start_against_an_unreachable_broker_raises_nats_error_once_the_timeout_expires():
    svc = _service(connect_timeout=0.5)
    tasks_before = asyncio.all_tasks()
    started = time.monotonic()

    with pytest.raises(NatsError, match=r"connect_timeout=0\.5"):
        await asyncio.wait_for(svc.start(), timeout=10)

    elapsed = time.monotonic() - started
    # Upper bound. CI p99 0.504 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.5 s,
    # 674x the overshoot; below 10 s (the outer wait_for).
    # Lower bound: the 0.5 s connect_timeout less slack; a start that failed at once falls under it.
    # Load can only lengthen it.
    assert 0.45 <= elapsed < 3.0, f"start() gave up after {elapsed:.2f}s, not at the 0.5s bound"
    await asyncio.sleep(0)
    assert svc._running is False
    assert svc.nc is None
    leftover = asyncio.all_tasks() - tasks_before
    assert not leftover, (
        f"a failed start left tasks running: {sorted(t.get_name() for t in leftover)}"
    )


async def test_CONTROL_start_against_a_dial_that_answers_does_not_raise_nats_error():
    """Otherwise the test above would pass for a `start()` that raised `NatsError` for any reason."""
    svc = _service(connect_timeout=0.5)

    async def refuses_for_another_reason(*args, **kwargs):
        raise ValueError("not a connection failure")

    with patch("cliffracer.core.dial.connect", side_effect=refuses_for_another_reason):
        with pytest.raises(Exception) as caught:
            await asyncio.wait_for(svc.start(), timeout=10)

    assert not isinstance(caught.value, NatsError), caught.value


def test_run_against_an_unreachable_broker_exits_non_zero_and_says_why():
    script = (
        "from cliffracer import CliffracerService, ServiceConfig\n"
        "svc = CliffracerService(ServiceConfig(name='rc_run_svc', health_port=0,\n"
        f"    nats_url='{DEAD_URL}', connect_timeout=1.0))\n"
        "svc.run()\n"
        "print('RUN RETURNED')\n"
    )

    done = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=60
    )

    assert done.returncode != 0, "run() came back from an unreachable broker as if it had run"
    assert "RUN RETURNED" not in done.stdout
    assert "nats.errors.Error" in done.stderr and "connect_timeout=1.0" in done.stderr, done.stderr[
        -2000:
    ]


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


def test_connect_timeout_defaults_to_thirty_seconds():
    """ADR-0012: the first connection is bounded by `connect_timeout`, default 30 seconds.

    The literal is the point. A validation test that carries 30.0 as a fixture value, or reads the
    default from the field, would pass with the default changed, so the default is asserted here
    by name and by number.
    """
    assert ServiceConfig(name="x").connect_timeout == 30.0
