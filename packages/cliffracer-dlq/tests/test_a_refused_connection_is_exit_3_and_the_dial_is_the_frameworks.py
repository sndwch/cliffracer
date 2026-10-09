"""A broker that turns the connection away is exit 3, named, and the dial is the framework's own.

`cliffracer-dlq` dialled with a bare `nats.connect` and caught a missing broker only. A broker that
answered and refused the login raised `nats.errors.Error`, which reached the user as a 33-line
traceback and exit 1, a code the README's exit table does not list, so a script switching on the
documented codes met an undocumented one. The dial is `cliffracer.core.dial.connect`, which bounds
the wait and closes the client a cut-off dial leaves behind, and a refusal is exit 3 with the
broker's reason and a hint at the credential flags.

The refusal is raised here as nats-py raises it from a login the broker refuses: the base
`nats.errors.Error` carrying the broker's own `-ERR` text.
"""

import pytest
from cliffracer_dlq import cli
from nats import errors

pytestmark = pytest.mark.unit

SERVER = "nats://ops:hunter2-canary@broker.example:4222"


def _dial_raises(monkeypatch, raised: BaseException) -> list[tuple[str, float]]:
    calls: list[tuple[str, float]] = []

    async def dial(url, *, timeout, **options):
        calls.append((url, timeout))
        raise raised

    monkeypatch.setattr("cliffracer.core.dial.connect", dial)
    return calls


def _run(*extra: str) -> int:
    return cli.main(["count", "--server", SERVER, "--timeout", "2.5", *extra])


def test_a_refused_login_is_exit_3_with_the_brokers_reason_and_no_traceback(monkeypatch, capsys):
    _dial_raises(monkeypatch, errors.Error("nats: 'Authorization Violation'"))

    code = _run("--password", "also-a-secret")

    err = capsys.readouterr().err
    assert code == 3, err
    assert "refused the connection" in err and "Authorization Violation" in err
    assert "--user, --password, --token or --creds" in err
    assert "Traceback" not in err
    assert "hunter2-canary" not in err and "also-a-secret" not in err
    assert "broker.example:4222" in err


def test_a_refusal_that_is_not_about_credentials_gets_no_credential_hint(monkeypatch, capsys):
    _dial_raises(monkeypatch, errors.Error("nats: 'Maximum Connections Exceeded'"))

    code = _run()

    err = capsys.readouterr().err
    assert code == 3, err
    assert "Maximum Connections Exceeded" in err and "--creds" not in err


@pytest.mark.parametrize(
    "raised",
    [errors.NoServersError(), errors.TimeoutError(), TimeoutError(), ConnectionRefusedError()],
    ids=["no-servers", "nats-timeout", "timeout", "refused-socket"],
)
def test_CONTROL_no_broker_at_the_address_is_still_exit_3_with_its_own_message(
    monkeypatch, capsys, raised
):
    _dial_raises(monkeypatch, raised)

    code = _run()

    err = capsys.readouterr().err
    assert code == 3 and "no broker answered at broker.example:4222" in err
    assert "refused the connection" not in err and "hunter2-canary" not in err


def test_the_dial_is_the_one_the_framework_uses_and_it_gets_the_commands_timeout(monkeypatch):
    calls = _dial_raises(monkeypatch, TimeoutError())

    _run()

    assert calls == [(SERVER, 2.5)]


@pytest.mark.parametrize(
    "server",
    [
        pytest.param("ops:hunter2-canary@broker.example:4222", id="no-scheme"),
        pytest.param("hunter2-canary@broker.example:4222", id="a-token-as-the-user-no-scheme"),
        pytest.param("nats://ops:hunter2-canary@broker.example:4222", id="with-a-scheme"),
    ],
)
@pytest.mark.parametrize(
    "raised",
    [TimeoutError(), errors.Error("nats: 'Authorization Violation'")],
    ids=["no-broker", "refused"],
)
def test_a_server_url_with_credentials_is_printed_without_them_with_or_without_a_scheme(
    monkeypatch, capsys, server, raised
):
    _dial_raises(monkeypatch, raised)

    code = cli.main(["count", "--server", server, "--timeout", "2"])

    err = capsys.readouterr().err
    assert code == 3, err
    assert "hunter2-canary" not in err, err
    assert "broker.example" in err
