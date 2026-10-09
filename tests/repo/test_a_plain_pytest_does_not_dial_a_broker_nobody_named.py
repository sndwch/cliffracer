"""A run that names no broker dials none: not to probe, not to run a test, not to clean up.

The suite probed `nats://localhost:4222` whenever `$CLIFFRACER_TEST_NATS_URL` was unset, and a
run that found a broker there used it. On a machine where that port is a broker other people
share, a plain `pytest` ran the `nats_required` tests against it, creating and deleting streams,
and the session cleanup connected to it at the end of every run rooted at `tests/`, whether or not
a broker test had run.

The broker here is a TCP listener the test owns and counts. The default address is pointed at it
by a plugin, so a run that dialled "the default broker" would reach it, and the count says whether
it did. Each case is a real child `pytest`, because what is under test is `pytest_configure`, the
collection hook and a session fixture, which a call into one function would not exercise.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.broker_isolation import PREFIX_ENV

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
TEST_BROKER_URL_ENV = "CLIFFRACER_TEST_NATS_URL"
PLUGIN = "default_broker_is_the_listener"
LISTENER_HOST = "127.0.0.1"  # this process's own listener, which is no suite address

# One file with a `nats_required` test, and one test that needs no broker and does not read the
# default address, which the plugin below moves to the listener. The marked test in that file
# starts a service on the default address and stops it, so it dials the listener and asks it for
# nothing the listener leaves unanswered: a JetStream call would wait out its own timeout.
A_BROKER_TEST_FILE = "tests/unit/test_first_connect_bounds.py"
A_BROKER_FREE_TEST = "tests/unit/test_service_config.py::TestServiceConfig::test_config_mutability"


class _CountingListener:
    """A TCP server standing in for a broker somebody else is using; it counts who dials it.

    It says just enough NATS to be connected to (an `INFO`, then `PONG` for every `PING`) and
    answers nothing else, so a client that connects and asks for something gives up on its own
    timeout. A listener that hung up would send a client into its reconnect schedule, and a test
    would wait minutes for it.
    """

    _INFO = b'INFO {"server_id":"fake","version":"2.10.0","proto":1,"max_payload":1048576}\r\n'

    def __enter__(self) -> _CountingListener:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind((LISTENER_HOST, 0))
        self._server.listen(16)
        self._server.settimeout(0.05)
        self.port: int = self._server.getsockname()[1]
        self.dials = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()
        return self

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.dials += 1
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        connection.settimeout(0.2)
        try:
            connection.sendall(self._INFO)
            while not self._stop.is_set():
                try:
                    data = connection.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    return
                if b"PING\r\n" in data:
                    connection.sendall(b"PONG\r\n")
        except OSError:
            return
        finally:
            connection.close()

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._server.close()

    @property
    def url(self) -> str:
        return f"nats://{LISTENER_HOST}:{self.port}"


def _default_broker_plugin(url: str) -> str:
    """A plugin that makes `url` the default broker address of the process that loads it.

    The rebuild is what makes `ServiceConfig()` take it: setting the field alone changes what
    `broker_url()` reads, and a `ServiceConfig()` built after still takes `nats://localhost:4222`.
    """
    return (
        "from cliffracer.core.service_config import ServiceConfig\n"
        f"ServiceConfig.model_fields['nats_url'].default = {url!r}\n"
        "ServiceConfig.model_rebuild(force=True)\n"
    )


def _run_pytest(
    listener: _CountingListener, tmp_path: Path, args: list[str], *, name_the_broker: bool
) -> subprocess.CompletedProcess[str]:
    """A child pytest whose DEFAULT broker address is the listener, with the variable set or not."""
    (tmp_path / f"{PLUGIN}.py").write_text(_default_broker_plugin(listener.url))
    # Neither variable is inherited: a prefix already exported makes the session cleanup return
    # before it dials, which would make the count below pass for the wrong reason.
    env = {k: v for k, v in os.environ.items() if k not in (TEST_BROKER_URL_ENV, PREFIX_ENV)}
    if name_the_broker:
        env[TEST_BROKER_URL_ENV] = listener.url
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(tmp_path), env.get("PYTHONPATH", "")]))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", PLUGIN, "-p", "no:cacheprovider", "-q", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_marked_test_is_held_back_and_the_default_broker_is_never_dialled(tmp_path):
    with _CountingListener() as listener:
        result = _run_pytest(
            listener, tmp_path, [A_BROKER_TEST_FILE, "-m", "nats_required"], name_the_broker=False
        )

    out = result.stdout
    assert listener.dials == 0, (
        f"a broker nobody named was dialled {listener.dials} time(s):\n{out}"
    )
    assert result.returncode == 0, out[-1500:]
    assert "1 skipped" in out and "passed" not in out.split("nats:")[0].splitlines()[-1], out
    assert "held back by the nats_required marker" in out, out


def test_a_run_of_tests_that_need_no_broker_still_dials_none_at_the_end(tmp_path):
    """The session cleanup connected at the end of every run rooted at `tests/`."""
    with _CountingListener() as listener:
        result = _run_pytest(listener, tmp_path, [A_BROKER_FREE_TEST], name_the_broker=False)

    assert result.returncode == 0, result.stdout[-1500:]
    assert listener.dials == 0, (
        f"the session cleanup dialled a broker nobody named ({listener.dials}):\n{result.stdout}"
    )


def test_the_probe_line_says_no_broker_was_named_and_what_to_set(tmp_path):
    with _CountingListener() as listener:
        result = _run_pytest(
            listener, tmp_path, [A_BROKER_TEST_FILE, "--collect-only"], name_the_broker=False
        )

    assert f"NO BROKER NAMED, not dialling {listener.url}" in result.stdout, result.stdout
    assert f"Set ${TEST_BROKER_URL_ENV}" in result.stdout, result.stdout
    assert "no broker named, not dialling" in result.stdout.splitlines()[-1], result.stdout


def test_CONTROL_a_broker_that_is_named_is_dialled_and_used(tmp_path):
    """The listener counts: a named broker is probed, so the zeros above are not a dead counter."""
    with _CountingListener() as listener:
        result = _run_pytest(
            listener, tmp_path, [A_BROKER_TEST_FILE, "--collect-only"], name_the_broker=True
        )

    assert listener.dials >= 1, result.stdout
    assert f"nats probe: broker at {listener.url}" in result.stdout, result.stdout
    assert "NO BROKER NAMED" not in result.stdout


def test_CONTROL_a_named_broker_is_not_held_back_by_the_marker(tmp_path):
    with _CountingListener() as listener:
        result = _run_pytest(
            listener, tmp_path, [A_BROKER_TEST_FILE, "--collect-only"], name_the_broker=True
        )

    assert "held back by the nats_required marker" not in result.stdout, result.stdout


def test_CONTROL_a_named_broker_is_still_swept_when_the_run_ends(tmp_path):
    """The cleanup returns early for a run that named nothing, and only for that run.

    A broker-free test is run, so nothing but the session cleanup can reach the broker after the
    probe: two dials, the probe and the cleanup, and the second is what a cleanup that never ran
    would not make.
    """
    with _CountingListener() as listener:
        result = _run_pytest(listener, tmp_path, [A_BROKER_FREE_TEST], name_the_broker=True)

    assert result.returncode == 0, result.stdout[-1500:]
    assert listener.dials >= 2, (
        f"only the probe dialled the named broker ({listener.dials}); the cleanup did not:\n"
        f"{result.stdout[-1200:]}"
    )


def test_the_plugin_moves_the_default_a_service_config_takes(tmp_path):
    """The address the plugin sets is the one `ServiceConfig()` takes, not only `broker_url()`.

    A child test that builds its own `ServiceConfig()` would otherwise dial the default broker,
    whatever the plugin set. A fresh interpreter, because the rebuild changes the class for the
    whole process.
    """
    with _CountingListener() as listener:  # an address of this test's own, dialled by nothing
        moved_to = listener.url
    (tmp_path / f"{PLUGIN}.py").write_text(_default_broker_plugin(moved_to))
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(filter(None, [str(tmp_path), os.environ.get("PYTHONPATH", "")])),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {PLUGIN}\n"
            "from cliffracer.core.service_config import ServiceConfig\n"
            "print(ServiceConfig(name='x', health_port=0).nats_url)\n",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == moved_to, result.stdout + result.stderr
