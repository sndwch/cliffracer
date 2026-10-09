"""`--server` names one server, and a list or a URL nats-py cannot dial is refused by name first.

nats-py dials one server per URL: given `nats://h1:4222,nats://h2:4222` it raises "invalid connect
url option" before it dials, and the CLI reported that as a broker that refused the connection, exit
3, naming no broker that had refused anything. `nats://h1:4222, nats://h2:4222` raised `ValueError`
out of `_host` as a traceback, exit 1. The check `ServiceConfig` makes of a `nats_url` is made of
`--server` before the dial, and a URL it refuses is a usage error, exit 7, naming the address
without its credentials and the reason, which never repeats the URL. `_host`, which names the
address in every message, reads each server from its redacted form and never raises.
"""

import os

import pytest
from cliffracer_dlq import cli
from nats import errors

from conftest import broker_url

pytestmark = pytest.mark.unit

PASSWORD = "hunter2-canary"


def _url(template: str) -> str:
    """A URL from a template, so this file holds no formatted broker address of its own."""
    return template.replace("<P>", PASSWORD)


def _dial_records(monkeypatch, raised: BaseException) -> list[str]:
    dialled: list[str] = []

    async def dial(url, *, timeout, **options):
        dialled.append(url)
        raise raised

    monkeypatch.setattr("cliffracer.core.dial.connect", dial)
    return dialled


def _run(server: str) -> int:
    return cli.main(["count", "--server", server, "--timeout", "2.5"])


LISTS = [
    pytest.param("nats://h1:4222,nats://h2:4223", "h1:4222,h2:4223", id="no-credentials"),
    pytest.param(
        _url("nats://ops:<P>@h1:4222,nats://h2:4223"), "h1:4222,h2:4223", id="creds-first"
    ),
    pytest.param(
        _url("nats://ops:<P>@h1:4222,nats://ops:<P>@h2:4223,tls://h3:4224"),
        "h1:4222,h2:4223,h3:4224",
        id="three-servers",
    ),
    pytest.param(
        _url("nats://svc:<P>,w@h1:4222,nats://svc:<P>,w@h2:4223"),
        "h1:4222,h2:4223",
        id="a-comma-in-a-password",
    ),
    pytest.param(
        _url("nats://ops:<P>@h1:4222/?token=<P>,tls://h2"), "h1:4222,h2", id="a-query-credential"
    ),
    pytest.param(_url("nats://ops:<P>@h1:4222, nats://h2:4223"), None, id="comma-space"),
]


def _refused(err: str) -> None:
    assert "is not a URL nats-py can dial" in err, err
    assert "Traceback" not in err and PASSWORD not in err, err


@pytest.mark.parametrize(("server", "named"), LISTS)
def test_a_list_of_servers_is_refused_by_name_before_it_is_dialled(
    monkeypatch, capsys, server, named
):
    dialled = _dial_records(monkeypatch, errors.NoServersError())

    with pytest.raises(SystemExit) as exited:
        _run(server)

    err = capsys.readouterr().err
    assert exited.value.code == cli.EXIT_USAGE, err
    assert dialled == []
    _refused(err)
    assert "one server per URL, no list" in err, err
    if named is not None:
        assert f"--server {named} is not" in err, err


@pytest.mark.parametrize(
    "server",
    [
        _url("ops:<P>@h1:4222,h2:4223"),
        "h1:4222,h2:4223",
        "nats://h1:70000",
        _url("nats://ops:<P>@h1:99999"),
        _url("nats://ops:a/<P>@h1:4222"),
    ],
    ids=["scheme-less-list", "scheme-less-plain-list", "port", "port-with-creds", "slash-in-pw"],
)
def test_a_url_nats_py_cannot_dial_is_refused_by_name_before_it_is_dialled(
    monkeypatch, capsys, server
):
    dialled = _dial_records(monkeypatch, errors.NoServersError())

    with pytest.raises(SystemExit) as exited:
        _run(server)

    err = capsys.readouterr().err
    assert exited.value.code == cli.EXIT_USAGE, err
    assert dialled == []
    _refused(err)


@pytest.mark.parametrize(
    "server",
    [
        _url("nats://ops:<P>@h1:4222, nats://h2:4223"),
        "nats://h1:4222, nats://h2:4223",
        "nats://h1:70000",
        _url("nats://ops:<P>@h1:99999,nats://h2:4222"),
        _url("nats://u:<P>@h1:4222,nats://h2:65536"),
        "",
        "nats://",
        _url("ops:<P>@h1:4222,h2:4223"),
    ],
)
def test_host_never_raises_and_never_prints_a_password(server):
    named = cli._host(server)

    assert PASSWORD not in named, named


@pytest.mark.parametrize(
    ("server", "named"),
    [
        (_url("nats://ops:<P>@broker.example:4222"), "broker.example:4222"),
        ("nats://broker.example:4222", "broker.example:4222"),
        ("nats://broker.example", "broker.example"),
        (_url("ops:<P>@broker.example:4222"), "***@broker.example:4222"),
        ("broker.example:4222", "broker.example:4222"),
    ],
)
def test_CONTROL_a_single_url_is_dialled_and_named_as_it_was(monkeypatch, capsys, server, named):
    dialled = _dial_records(monkeypatch, errors.NoServersError())

    code = _run(server)

    err = capsys.readouterr().err
    assert code == 3 and f"no broker answered at {named}\n" in err, err
    assert dialled == [server]
    assert PASSWORD not in err


def _live_server() -> str:
    return os.environ.get("CLIFFRACER_TEST_NATS_URL") or broker_url()


@pytest.mark.nats_required
@pytest.mark.parametrize("live_first", [True, False], ids=["live-first", "live-second"])
def test_a_list_holding_a_live_server_is_refused_by_name(capsys, live_first):
    """Refused before any dial, wherever the reachable server sits in the list."""
    dead = _live_server().rsplit(":", 1)[0] + ":1"  # the live host, on a port nothing listens on
    servers = [_live_server(), dead] if live_first else [dead, _live_server()]

    with pytest.raises(SystemExit) as exited:
        cli.main(["count", "--server", ",".join(servers), "--timeout", "2"])

    err = capsys.readouterr().err
    assert exited.value.code == cli.EXIT_USAGE, err
    _refused(err)


@pytest.mark.nats_required
def test_CONTROL_the_live_server_alone_is_dialled(capsys):
    code = cli.main(["count", "--server", _live_server(), "--timeout", "2"])

    err = capsys.readouterr().err
    assert code not in (cli.EXIT_NO_BROKER, cli.EXIT_USAGE), err


def test_host_names_no_server_written_inside_a_password_that_follows_a_host():
    """`ops:<P>` after `h1:4222,` starts a password running to the next `@`; `nats://h9:4222`
    inside it is not a server."""
    named = cli._host(_url("nats://a@h1:4222,ops:<P>,nats://h9:4222,nats://u2@h2:4222"))

    assert named == "h1:4222,***@h2:4222"


def test_host_names_no_server_written_inside_a_token_that_starts_the_list_with_a_comma():
    assert cli._host(_url(",nats://h9:4222,NATS://<P>@hjr5:4222")) == "***@hjr5:4222"


def test_host_reads_a_server_from_its_redacted_form_so_no_part_of_a_password_is_a_host():
    """`urlsplit` of the raw URL reads `ops:1234` as host and port when the password holds a '/'."""
    assert cli._host(_url("nats://ops:1234/<P>@h1:4222")) == "h1:4222"
