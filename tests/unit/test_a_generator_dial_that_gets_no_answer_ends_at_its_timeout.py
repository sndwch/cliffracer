"""The generator's `--timeout` bounds the whole dial, and a failed dial prints one line.

`--timeout` was handed to nats-py as `connect_timeout`, which bounds one attempt, with one
reconnect attempt after it: against a listener that accepts and says nothing, `--timeout 1` took
2.9 seconds. And nats-py's default error callback logged each error with its traceback, so the
one-line message `main` prints came after about 65 lines of traceback.
"""

import socket
import subprocess
import sys
import time

import pytest

from cliffracer.generate_client.cli import main

pytestmark = pytest.mark.unit

HOST = "127.0.0.1"


@pytest.fixture
def silent_listener():
    """A listener that accepts connections into its backlog and never says anything."""
    listener = socket.socket()
    listener.bind((HOST, 0))
    listener.listen(8)
    yield f"nats://{HOST}:{listener.getsockname()[1]}"
    listener.close()


@pytest.fixture
def closed_port():
    """An address nothing listens on."""
    holder = socket.socket()
    holder.bind((HOST, 0))
    url = f"nats://{HOST}:{holder.getsockname()[1]}"
    holder.close()
    return url


def test_a_dial_that_gets_no_answer_ends_at_the_timeout_and_exits_3(silent_listener, capsys):
    started = time.monotonic()
    rc = main(["--service", "orders", "--nats-url", silent_listener, "--timeout", "1"])
    elapsed = time.monotonic() - started

    assert rc == 3
    # Upper bound. CI p99 1 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 1 s, 187x
    # the overshoot.
    assert elapsed < 1.9, f"--timeout 1 took {elapsed:.2f}s to give up on the broker"
    err = capsys.readouterr().err
    assert "no broker reachable at" in err and "did not answer within 1.0s" in err, err


def test_CONTROL_a_longer_timeout_is_waited_for(silent_listener):
    """Without this, "ends at the timeout" could mean "ends at once"."""
    started = time.monotonic()
    rc = main(["--service", "orders", "--nats-url", silent_listener, "--timeout", "1.5"])

    assert rc == 3
    # Lower bound: the 1.5 s timeout less 0.1 s; a dial that gave up at once, or at 1 s, falls under
    # it. Load can only lengthen it.
    assert time.monotonic() - started >= 1.4


def _one_line_and_no_traceback(url: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cliffracer.generate_client.cli",
            "--service",
            "orders",
            "--nats-url",
            url,
            "--timeout",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    lines = result.stderr.splitlines()
    assert result.returncode == 3, result.stderr
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith("no broker reachable at "), lines
    assert "Traceback" not in result.stderr


def test_a_dial_that_gets_no_answer_prints_one_line_and_no_traceback(silent_listener):
    _one_line_and_no_traceback(silent_listener)


def test_a_dial_that_is_refused_prints_one_line_and_no_traceback(closed_port):
    _one_line_and_no_traceback(closed_port)
