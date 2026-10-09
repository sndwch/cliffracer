"""`cliffracer-generate-client` says when the broker refused it, and can use a confined inbox.

A refused login ended the dial with `NoServersError`, so it was reported as "no broker reachable".
A client role the broker confines to its own inbox prefix (`docs/broker-permissions.md`) cannot
subscribe to the generator's default reply inbox: the broker reports the violation through the
error callback and the request times out, so it was reported as "no service answered", and the
command had no way to name the inbox prefix. Both are now exit 3 with the broker's reason, and
`--inbox-prefix` names the prefix.
"""

import types

import nats.errors
import pytest

from cliffracer.generate_client.cli import main

pytestmark = pytest.mark.unit

URL = "nats://user:pw@broker.example:5222"
VIOLATION = nats.errors.Error("nats: 'Permissions Violation for Subscription to \"_inbox.abc.*\"'")


def _run(argv):
    return main(["--service", "orders", "--nats-url", URL, "--timeout", "1", *argv])


def test_a_refused_login_is_exit_3_naming_the_credentials_not_a_missing_broker(monkeypatch, capsys):
    async def connect(url, **kwargs):
        await kwargs["error_cb"](nats.errors.Error("nats: 'Authorization Violation'"))
        raise nats.errors.NoServersError()

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)

    rc = _run([])

    err = capsys.readouterr().err
    assert rc == 3
    assert "refused this client's credentials" in err, err
    assert "Authorization Violation" in err
    assert "no broker reachable" not in err
    assert "pw" not in err.replace("password", ""), err


def test_a_permissions_violation_on_the_reply_inbox_is_exit_3_pointing_at_inbox_prefix(
    monkeypatch, capsys
):
    class Connection:
        async def request(self, subject, payload, timeout, headers=None):
            await seen["error_cb"](VIOLATION)
            raise nats.errors.TimeoutError()

        async def close(self):
            pass

    seen = {}

    async def connect(url, **kwargs):
        seen.update(kwargs)
        return Connection()

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)

    rc = _run([])

    err = capsys.readouterr().err
    assert rc == 3
    assert "refused this client a permission" in err and "Permissions Violation" in err, err
    assert "--inbox-prefix" in err
    assert "no service" not in err


def test_CONTROL_a_timeout_with_no_violation_is_still_a_service_that_did_not_answer(
    monkeypatch, capsys
):
    class Connection:
        async def request(self, subject, payload, timeout, headers=None):
            raise nats.errors.TimeoutError()

        async def close(self):
            pass

    async def connect(url, **kwargs):
        return Connection()

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)

    rc = _run([])

    assert rc == 2
    assert "no service 'orders' answered" in capsys.readouterr().err


def test_CONTROL_an_unrelated_error_before_a_timeout_is_still_a_service_that_did_not_answer(
    monkeypatch, capsys
):
    """An error the broker reports that is not a refusal does not turn a timeout into exit 3."""

    class Connection:
        async def request(self, subject, payload, timeout, headers=None):
            await seen["error_cb"](nats.errors.Error("nats: some unrelated async error"))
            raise nats.errors.TimeoutError()

        async def close(self):
            pass

    seen = {}

    async def connect(url, **kwargs):
        seen.update(kwargs)
        return Connection()

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)

    rc = _run([])

    err = capsys.readouterr().err
    assert rc == 2
    assert "no service 'orders' answered" in err
    assert "permission" not in err and "credentials" not in err


def test_CONTROL_a_dial_that_fails_for_another_reason_is_still_no_broker(monkeypatch, capsys):
    async def connect(url, **kwargs):
        await kwargs["error_cb"](nats.errors.Error("nats: something else"))
        raise nats.errors.NoServersError()

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)

    rc = _run([])

    assert rc == 3
    assert "no broker reachable" in capsys.readouterr().err


def test_inbox_prefix_is_passed_to_the_connection_and_only_when_given(monkeypatch):
    seen = []

    async def connect(url, **kwargs):
        seen.append(kwargs)
        return types.SimpleNamespace(request=_reply, close=_close)

    async def _reply(subject, payload, timeout, headers=None):
        raise nats.errors.TimeoutError()

    async def _close():
        pass

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)

    _run(["--inbox-prefix", "_INBOX.customers"])
    _run([])

    assert seen[0]["inbox_prefix"] == "_INBOX.customers"
    assert "inbox_prefix" not in seen[1]


@pytest.mark.parametrize("prefix", ["_INBOX", "$SYS.x", "has space", "a..b", "*"])
def test_a_prefix_the_framework_would_refuse_is_a_usage_error(prefix, capsys):
    with pytest.raises(SystemExit) as exit_info:
        _run(["--inbox-prefix", prefix])

    assert exit_info.value.code == 7
    assert "--inbox-prefix: " in capsys.readouterr().err


def test_inbox_prefix_applies_only_without_class(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--service", "orders", "--class", "x:y", "--inbox-prefix", "_INBOX.customers"])

    assert exit_info.value.code == 7
    assert "--inbox-prefix applies only without --class" in capsys.readouterr().err
