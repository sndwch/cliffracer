"""`cliffracer-generate-client` names the broker it could not use without the credentials in its URL.

The URL is the only way the command carries broker credentials, and an error message is what lands
in a CI log. Every other place that dials a broker redacts the URL before printing it
(`redact_nats_url`); the generator printed `--nats-url` and `$CLIFFRACER_NATS_URL` as given in the
three messages that name a broker, one for exit 2 and two for exit 3.
"""

import nats.errors
import pytest

from cliffracer.generate_client.cli import main

pytestmark = pytest.mark.unit

HOST = "broker.example:5222"
SECRETS = ["s3cretpw", "tokenvalue"]
URLS = [
    f"nats://alice:s3cretpw@{HOST}",
    f"nats://tokenvalue@{HOST}",
    f"tls://alice:s3cretpw@{HOST}",
]
FAILURES = [
    (nats.errors.NoRespondersError(), 2),
    (nats.errors.TimeoutError(), 2),
    (nats.errors.NoServersError(), 3),
    (ConnectionRefusedError("refused"), 3),
    (OSError("unreachable"), 3),
    (nats.errors.Error("some other nats error"), 3),
]


def _fails_with(monkeypatch, failure):
    async def fetch(*args, **kwargs):
        raise failure

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)


@pytest.mark.parametrize("url", URLS)
@pytest.mark.parametrize(("failure", "code"), FAILURES, ids=[type(f).__name__ for f, _ in FAILURES])
def test_a_message_naming_the_broker_does_not_carry_its_credentials(
    monkeypatch, capsys, url, failure, code
):
    _fails_with(monkeypatch, failure)

    rc = main(["--service", "orders", "--nats-url", url, "--timeout", "1.0"])

    err = capsys.readouterr().err
    assert rc == code, err
    assert HOST in err, err
    for secret in SECRETS:
        assert secret not in err, err


@pytest.mark.parametrize(("failure", "code"), FAILURES, ids=[type(f).__name__ for f, _ in FAILURES])
def test_the_environment_variable_is_redacted_the_same_way(monkeypatch, capsys, failure, code):
    _fails_with(monkeypatch, failure)
    monkeypatch.setenv("CLIFFRACER_NATS_URL", URLS[0])

    rc = main(["--service", "orders", "--timeout", "1.0"])

    err = capsys.readouterr().err
    assert rc == code, err
    assert HOST in err and "s3cretpw" not in err, err


def test_CONTROL_a_url_without_credentials_is_printed_as_given(monkeypatch, capsys):
    """Without this, "redacted" could mean "the URL is dropped from the message"."""
    _fails_with(monkeypatch, nats.errors.NoServersError())

    rc = main(["--service", "orders", "--nats-url", f"nats://{HOST}", "--timeout", "1.0"])

    assert rc == 3
    assert f"no broker reachable at nats://{HOST}:" in capsys.readouterr().err
