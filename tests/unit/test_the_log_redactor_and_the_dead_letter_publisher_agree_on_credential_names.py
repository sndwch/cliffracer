"""A name that one of them treats as a credential, the other does too.

The NATS log stream and the dead-letter record publish to readers the service did not choose, and
each decided by its own list which names carry a credential. They disagreed: the dead-letter
publisher withheld `cookie`, `set-cookie`, `jwt`, `bearer` and `session*` headers, and the log
redactor published a record's `cookie`, `jwt`, `bearer` and `session_id` values and its
`secret_key`, `signing_key` and `passphrase`. Each name is run through both, on the public
surfaces: a log record through `redact_sensitive_log_fields`, a header through
`DeadLetterPublisher.origin`.
"""

from types import SimpleNamespace

import pytest
from cliffracer_logging import redact_sensitive_log_fields

from cliffracer import ServiceConfig
from cliffracer.core.dispatch.dlq import DeadLetterPublisher

pytestmark = pytest.mark.unit

CREDENTIAL_NAMES = [
    "password", "authorization", "api_key", "x-api-key", "secret", "client_secret", "token",
    "access_token", "refresh_token", "private_key", "access_key", "credentials", "apikey",
    "secret_key", "jwt", "cookie", "Set-Cookie", "session_id", "sessionid", "bearer",
    "authorization_header", "signing_key", "encryption_key", "passphrase", "Bearer-Token",
]  # fmt: skip
ORDINARY_NAMES = ["customer_id", "x-request-id", "content-type", "trace_id", "message", "user"]


def _redacted_in_a_log_record(name: str) -> bool:
    record = redact_sensitive_log_fields({"extra": {name: "value"}})
    return record["extra"][name] == "[REDACTED]"


def _withheld_from_a_dead_letter(name: str, *, configured=()) -> bool:
    ext = [SimpleNamespace(header=h) for h in configured]
    service = SimpleNamespace(container=SimpleNamespace(extensions=ext))
    publisher = DeadLetterPublisher(ServiceConfig(name="orders"), lambda: None, service=service)
    fields, _ = publisher.origin(SimpleNamespace(headers={name: "value"}))
    return name in fields.get("withheld_headers", [])


@pytest.mark.parametrize("name", CREDENTIAL_NAMES)
def test_a_credential_name_is_redacted_in_a_log_record(name):
    assert _redacted_in_a_log_record(name)


@pytest.mark.parametrize("name", CREDENTIAL_NAMES)
def test_a_credential_name_is_withheld_from_a_dead_letter(name):
    assert _withheld_from_a_dead_letter(name)


@pytest.mark.parametrize("name", ORDINARY_NAMES)
def test_CONTROL_an_ordinary_name_is_kept_by_both(name):
    assert not _redacted_in_a_log_record(name)
    assert not _withheld_from_a_dead_letter(name)


def test_the_header_an_extension_reads_is_withheld_from_a_dead_letter():
    assert not _withheld_from_a_dead_letter("x-svc-id")
    assert _withheld_from_a_dead_letter("x-svc-id", configured=["x-svc-id"])


def test_the_default_redactor_given_that_header_redacts_it():
    plain = redact_sensitive_log_fields({"extra": {"x-svc-id": "v"}})
    named = redact_sensitive_log_fields(
        {"extra": {"x-svc-id": "v"}}, credential_names=frozenset({"x-svc-id"})
    )
    assert plain["extra"]["x-svc-id"] == "v"
    assert named["extra"]["x-svc-id"] == "[REDACTED]"
