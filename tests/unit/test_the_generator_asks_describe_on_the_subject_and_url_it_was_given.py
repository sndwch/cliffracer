"""The generator's describe request goes to the subject and the broker it was told to use.

Both exit-2 tests replace `fetch_description` outright, and the namespace tests replace it with a
function that never builds a subject, so the construction of `{namespace}.{service}.describe` and
the reading of `$CLIFFRACER_NATS_URL` ran in no unit test: the generator could ignore `--namespace`
and the environment variable and the suite stayed green. These run the real `fetch_description`
over a connection that records the request, and the real `main` over a `fetch_description` that
records the address.
"""

import asyncio
import types

import nats.errors
import pytest

from cliffracer.generate_client import cli
from cliffracer.generate_client.cli import fetch_description, main

pytestmark = pytest.mark.unit


class _Connection:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str] | None]] = []

    async def request(self, subject, payload, *, timeout, headers=None):
        self.requests.append((subject, headers))
        return types.SimpleNamespace(data=b"{}", headers={})

    async def close(self):
        return None


def _connect_to(monkeypatch) -> _Connection:
    connection = _Connection()

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)
    return connection


@pytest.mark.parametrize(
    ("namespace", "prefix", "subject"),
    [
        (None, None, "orders.describe"),
        ("ns1", None, "ns1.orders.describe"),
        (None, "prod", "prod.orders.describe"),
        ("ns1", "prod", "prod.ns1.orders.describe"),
    ],
)
def test_the_describe_request_goes_to_the_subject_the_service_answers_on(
    monkeypatch, namespace, prefix, subject
):
    connection = _connect_to(monkeypatch)
    if prefix is None:
        monkeypatch.delenv("CLIFFRACER_SUBJECT_PREFIX", raising=False)
    else:
        monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", prefix)

    asyncio.run(fetch_description("nats://a:1", "orders", namespace, 1.0))

    assert [asked for asked, _ in connection.requests] == [subject]


def test_the_request_carries_the_headers_it_was_given(monkeypatch):
    connection = _connect_to(monkeypatch)

    asyncio.run(fetch_description("nats://a:1", "orders", None, 1.0, {"authorization": "bearer t"}))

    assert connection.requests == [("orders.describe", {"authorization": "bearer t"})]


def _record_the_address(monkeypatch) -> list[str]:
    seen: list[str] = []

    async def fetch(url, service, namespace, timeout, headers=None):
        seen.append(url)
        raise nats.errors.NoServersError()

    monkeypatch.setattr(cli, "fetch_description", fetch)
    return seen


def test_main_dials_the_address_in_the_environment_when_no_flag_gives_one(monkeypatch, capsys):
    seen = _record_the_address(monkeypatch)
    monkeypatch.setenv("CLIFFRACER_NATS_URL", "nats://from-env:4222")

    code = main(["--service", "orders"])

    assert (code, seen) == (3, ["nats://from-env:4222"])
    assert "nats://from-env:4222" in capsys.readouterr().err


def test_main_prefers_the_flag_to_the_environment(monkeypatch):
    seen = _record_the_address(monkeypatch)
    monkeypatch.setenv("CLIFFRACER_NATS_URL", "nats://from-env:4222")

    main(["--service", "orders", "--nats-url", "nats://from-flag:4222"])

    assert seen == ["nats://from-flag:4222"]


def test_main_falls_back_to_the_default_address_with_neither(monkeypatch):
    seen = _record_the_address(monkeypatch)
    monkeypatch.delenv("CLIFFRACER_NATS_URL", raising=False)

    main(["--service", "orders"])

    assert seen == [cli.DEFAULT_URL]


def test_main_hands_the_namespace_and_the_headers_to_the_request(monkeypatch):
    received: list[tuple[str | None, dict[str, str]]] = []

    async def fetch(url, service, namespace, timeout, headers=None):
        received.append((namespace, headers))
        raise nats.errors.NoServersError()

    monkeypatch.setattr(cli, "fetch_description", fetch)

    main(["--service", "orders", "--namespace", "ns1", "--header", "authorization=bearer t"])

    assert received == [("ns1", {"authorization": "bearer t"})]
