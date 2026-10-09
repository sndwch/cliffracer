"""A run connects with what it was given, reads, and closes the connection on every path.

The connection is a fake `cliffracer.core.dial.connect` that records what it was given and answers the one stream
names request; the stream manager behind it answers the reads.
"""

import json
from types import SimpleNamespace

import pytest
from cliffracer_dlq import cli
from nats.errors import NoRespondersError
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import RawStreamMsg
from nats.js.errors import APIError, ServiceUnavailableError

pytestmark = pytest.mark.unit


class FakeConnection:
    def __init__(self, streams, names=("DLQ",), request_fails=None):
        self._streams = streams
        self.names = list(names)
        self.request_fails = request_fails
        self.closed = False
        self.jsm_options: dict = {}
        self.asked: list[str] = []

    async def request(self, subject, payload=b"", timeout=None):
        self.asked.append(subject)
        if self.request_fails:
            raise self.request_fails
        return SimpleNamespace(data=json.dumps({"streams": self.names}).encode())

    def jsm(self, **options):
        self.jsm_options = options
        return self._streams

    async def close(self):
        self.closed = True


class Streams:
    def __init__(self, *, fail=None):
        self.fail = fail

    async def stream_info(self, name):
        if self.fail:
            raise self.fail
        return SimpleNamespace(state=SimpleNamespace(messages=1, first_seq=1, last_seq=1))

    async def get_msg(self, stream_name, seq=None, subject=None, direct=False, next=False):
        record = {"service": "orders", "deliveries": 3, "error": "boom", "original_subject": "e.x"}
        return RawStreamMsg(
            subject="dlq.orders",
            seq=1,
            data=json.dumps(record).encode(),
            headers={"Content-Type": "application/json"},
            time=None,
        )


def _patch_connect(monkeypatch, streams, **connection_options):
    connection = FakeConnection(streams, **connection_options)
    seen: dict = {}

    async def connect(url, **options):
        seen.update(options, servers=[url])
        return connection

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)
    return connection, seen


def test_a_run_connects_with_what_it_was_given_reads_and_closes_the_connection(monkeypatch, capsys):
    connection, seen = _patch_connect(monkeypatch, Streams())

    code = cli.main(
        [
            "ls",
            "--server",
            "nats://broker.example:4222",
            "--creds",
            "/etc/ops.creds",
            "--user",
            "ops",
            "--password",
            "pw",
            "--timeout",
            "3",
        ]
    )

    assert code == 0 and connection.closed
    assert seen["servers"] == ["nats://broker.example:4222"]
    assert (seen["user_credentials"], seen["user"], seen["password"]) == (
        "/etc/ops.creds",
        "ops",
        "pw",
    )
    assert "token" not in seen
    assert connection.jsm_options == {"timeout": 3.0}
    assert "orders" in capsys.readouterr().out


def test_the_connection_is_read_from_the_environment_the_nats_command_uses(monkeypatch):
    monkeypatch.setenv("NATS_URL", "nats://from-env:4222")
    monkeypatch.setenv("NATS_TOKEN", "tok")
    connection, seen = _patch_connect(monkeypatch, Streams())

    assert cli.main(["count"]) == 0

    assert seen["servers"] == ["nats://from-env:4222"] and seen["token"] == "tok"
    assert connection.closed


@pytest.mark.parametrize(
    "failure", [NatsTimeoutError(), NoRespondersError(), ServiceUnavailableError()]
)
def test_a_stream_request_the_broker_does_not_answer_exits_6_and_still_closes_the_connection(
    monkeypatch, capsys, failure
):
    connection, _ = _patch_connect(monkeypatch, Streams(fail=failure))

    code = cli.main(["ls", "--stream", "DLQ"])

    assert code == 6 and connection.closed
    assert "did not answer a stream request" in capsys.readouterr().err


def test_a_stream_request_the_broker_refuses_exits_6_with_the_refusal_named(monkeypatch, capsys):
    connection, _ = _patch_connect(
        monkeypatch, Streams(fail=APIError(code=403, description="not authorized"))
    )

    code = cli.main(["ls", "--stream", "DLQ"])

    assert code == 6 and connection.closed
    assert "the broker refused a stream request" in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure", [NoRespondersError(), ServiceUnavailableError(), NatsTimeoutError()]
)
def test_finding_the_stream_while_jetstream_is_off_exits_6_not_4(monkeypatch, failure):
    connection, _ = _patch_connect(monkeypatch, Streams(), request_fails=failure)

    code = cli.main(["ls"])

    assert code == 6 and connection.closed


def test_a_wildcard_that_several_streams_hold_exits_4_through_the_whole_run(monkeypatch, capsys):
    connection, _ = _patch_connect(monkeypatch, Streams(), names=("DLQ_B", "DLQ_A"))

    code = cli.main(["count", "--subject", "dlq.*"])

    captured = capsys.readouterr()
    assert code == 4 and connection.closed and captured.out == ""
    assert "2 streams hold the subject 'dlq.*': DLQ_A, DLQ_B" in captured.err
    assert connection.asked == ["$JS.API.STREAM.NAMES"]


def test_naming_the_stream_asks_the_broker_for_no_names(monkeypatch):
    connection, _ = _patch_connect(monkeypatch, Streams(), names=("DLQ_A", "DLQ_B"))

    assert cli.main(["count", "--stream", "DLQ"]) == 0

    assert connection.asked == []
