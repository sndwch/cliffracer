"""The generator's describe request closes its connection without a say in the exit code.

`fetch_description` drained in a `finally`. `drain` raises on a connection that has already
dropped, and an exception from a `finally` replaces the one in flight, so a broker that went away
mid-request was reported as whatever the teardown said and not as the request's own failure, which
`main` sorts into its documented exit codes. It closes, which does not raise on a closed
connection, and the request's error is the one reported.
"""

import asyncio
import types

import nats.errors
import pytest

from cliffracer import ServiceConfig
from cliffracer.generate_client.cli import fetch_description, main

pytestmark = pytest.mark.unit


class _Connection:
    def __init__(self, *, raises: BaseException | None = None, data: bytes = b"{}") -> None:
        self.raises, self.data = raises, data
        self.closed = 0
        self.drained = 0

    async def request(self, *args, **kwargs):
        if self.raises is not None:
            raise self.raises
        return types.SimpleNamespace(data=self.data, headers={})

    async def close(self):
        self.closed += 1

    async def drain(self):
        self.drained += 1
        raise nats.errors.ConnectionClosedError()


def _connect_to(monkeypatch, connection):
    async def connect(*args, **_):
        return connection

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)


def test_the_requests_own_error_is_the_one_raised_and_the_connection_is_closed(monkeypatch):
    connection = _Connection(raises=TimeoutError("no reply"))
    _connect_to(monkeypatch, connection)

    with pytest.raises(TimeoutError, match="no reply"):
        asyncio.run(fetch_description("nats://x", "orders", None, 1.0))

    assert (connection.closed, connection.drained) == (1, 0)


def test_CONTROL_a_reply_is_returned_and_the_connection_is_closed_once(monkeypatch):
    connection = _Connection(data=b'{"service": "orders"}')
    _connect_to(monkeypatch, connection)

    assert (
        asyncio.run(fetch_description("nats://x", "orders", None, 1.0)) == b'{"service": "orders"}'
    )
    assert (connection.closed, connection.drained) == (1, 0)


@pytest.mark.parametrize(
    "raises",
    [nats.errors.NoServersError(), nats.errors.ConnectionClosedError()],
    ids=["no-servers", "closed-mid-request"],
)
def test_a_broker_error_still_exits_with_the_code_for_a_broker_error(monkeypatch, capsys, raises):
    connection = _Connection(raises=raises)
    _connect_to(monkeypatch, connection)

    url = ServiceConfig.model_fields["nats_url"].default
    code = main(["--service", "orders", "--nats-url", url])

    assert code == 3, capsys.readouterr().err
    assert connection.drained == 0
