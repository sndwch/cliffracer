"""The health listener reads a request to its end, answers it whole, and keeps nothing after.

The request's headers end at a blank line, a bare `\\n` as much as `\\r\\n`, or where the client
stops sending; a request with far more header lines than `MAX_HEADER_LINES` is a 431. Only GET is
served. Every answer names its length and closes the connection, and a body that holds a value
JSON has no form for, such as a datetime, is sent with that value as text. `/info` adds the bound
port to a copy of the service's info, not to the service's own dict. A probe that crashes is
logged with its detail, which the answer withholds. A served connection is forgotten when it
closes, and a port already in use is logged as one before the bind's error is raised.
"""

import asyncio
import datetime
import json
import socket

import pytest
from loguru import logger

from cliffracer.core import health_listener as health_listener_module
from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit

NEVER = 10.0


class Probed:
    class config:
        name = "probed"
        health_port = 0
        expose_internal_errors = False

    def __init__(self) -> None:
        self.info = {"service": "probed"}
        self.crash = False

    async def health_check(self) -> dict:
        if self.crash:
            raise RuntimeError("the database password is hunter2")
        return {"status": "healthy", "checked_at": datetime.datetime(2026, 1, 2, 3, 4, 5)}

    def get_service_info(self) -> dict:
        return self.info


@pytest.fixture
async def served():
    service = Probed()
    listener = HealthListener(service, "127.0.0.1")
    await listener.start()
    try:
        yield service, listener
    finally:
        await listener.stop()


@pytest.fixture
def warnings():
    seen: list[str] = []
    sink = logger.add(
        lambda m: seen.append(str(m).rstrip("\n")), level="WARNING", format="{message}"
    )
    yield seen
    logger.remove(sink)


async def _send(port: int, request: bytes, *, then_close: bool = False) -> tuple[int, dict, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    if then_close:
        writer.write_eof()
    raw = await asyncio.wait_for(reader.read(), NEVER)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body), head


@pytest.mark.parametrize(("lines", "status"), [(10, 200), (1000, 431)])
async def test_a_request_with_far_more_header_lines_than_the_cap_is_a_431(served, lines, status):
    _, listener = served
    request = b"GET /ready HTTP/1.1\r\n" + b"X-Filler: y\r\n" * lines + b"\r\n"

    assert (await _send(listener.port, request))[0] == status


async def test_headers_ended_by_a_bare_newline_are_answered_without_waiting(served, monkeypatch):
    """Answered at once, not when the request deadline passes, which answers 500."""
    monkeypatch.setattr(health_listener_module, "REQUEST_DEADLINE_SECONDS", 1.0)
    _, listener = served

    status, body, _ = await _send(listener.port, b"GET /ready HTTP/1.1\nHost: x\n\n")

    assert (status, body["status"]) == (200, "healthy")


async def test_a_request_line_then_the_end_of_the_stream_is_answered(served):
    _, listener = served

    status, body, _ = await _send(listener.port, b"GET /ready HTTP/1.1\r\n", then_close=True)

    assert (status, body["status"]) == (200, "healthy")


async def test_head_is_refused_like_any_method_but_get(served):
    _, listener = served

    assert (await _send(listener.port, b"HEAD /ready HTTP/1.1\r\n\r\n"))[0] == 405


async def test_a_value_json_has_no_form_for_is_sent_as_text(served):
    _, listener = served

    status, body, _ = await _send(listener.port, b"GET /ready HTTP/1.1\r\n\r\n")

    assert (status, body["checked_at"]) == (200, "2026-01-02 03:04:05")


async def test_an_answer_names_its_length_and_closes_the_connection(served):
    _, listener = served
    reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
    writer.write(b"GET /ready HTTP/1.1\r\n\r\n")
    raw = await asyncio.wait_for(reader.read(), NEVER)
    writer.close()

    head, _, body = raw.partition(b"\r\n\r\n")
    headers = head.decode().split("\r\n")[1:]

    assert f"Content-Length: {len(body)}" in headers
    assert "Connection: close" in headers


async def test_info_adds_the_port_to_a_copy_of_the_services_info(served):
    service, listener = served

    _, body, _ = await _send(listener.port, b"GET /info HTTP/1.1\r\n\r\n")

    assert body["health_port"] == listener.port
    assert service.info == {"service": "probed"}


async def test_a_crashed_probe_is_logged_with_its_detail_the_answer_withholds(served, warnings):
    service, listener = served
    service.crash = True

    status, body, _ = await _send(listener.port, b"GET /ready HTTP/1.1\r\n\r\n")

    assert status == 503
    assert "hunter2" not in json.dumps(body)
    assert warnings == ["health listener request failed: the database password is hunter2"]


async def test_a_served_connection_is_forgotten_when_it_closes(served):
    _, listener = served
    for _ in range(3):
        await _send(listener.port, b"GET /ready HTTP/1.1\r\n\r\n")

    async def forgotten() -> None:
        while listener._connections:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(forgotten(), NEVER)


async def test_a_port_already_in_use_is_logged_as_one_and_the_bind_error_raised(monkeypatch):
    # The port is one this test holds itself, on loopback, so no shared port is bound.
    monkeypatch.setattr(HealthListener, "_test_port_override", None)
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    service = Probed()
    service.config = type("config", (), {"name": "probed", "health_port": port})
    errors: list[str] = []
    sink = logger.add(
        lambda m: errors.append(str(m).rstrip("\n")), level="ERROR", format="{message}"
    )
    try:
        with pytest.raises(OSError):
            await HealthListener(service, "127.0.0.1").start()
    finally:
        logger.remove(sink)
        held.close()

    assert errors == [f"health listener failed to bind 127.0.0.1:{port}: port already in use"]
