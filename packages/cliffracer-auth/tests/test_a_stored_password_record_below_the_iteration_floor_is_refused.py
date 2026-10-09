"""A stored PBKDF2 record made at fewer iterations than the floor is refused, and says why.

A record carrying one iteration verified: `verify_password` refused only a count of zero or one
over the ceiling, and `AuthConfig` accepted a count of one for new hashes, so nothing stated a
lower bound. The floor, `FLOOR`, is now the fewest a record may carry and the fewest
a service may be configured for, the way the ceiling is the most for both.

`verify_password` returns `False` and cannot raise, so the reason is a warning that names the
record's count and the floor. A refused record spends one PBKDF2 at the configured count, as a name
that is not registered does, so how long a login takes does not say which users hold such a record.
The work done is counted, since the time is not something to assert.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService
from loguru import logger
from pydantic import ValidationError

pytestmark = pytest.mark.unit

SECRET = "x" * 40
PASSWORD = "a-password-of-enough-length"
CONFIGURED = 2_000
# The documented floor, written out so these read what a user is told. One test pins that the
# constant the config and the verifier share is this number.
FLOOR = 1_000


def _service(iterations: int = CONFIGURED) -> SimpleAuthService:
    return SimpleAuthService(AuthConfig(secret_key=SECRET, pbkdf2_iterations=iterations))


def _record(password: str, iterations: int) -> str:
    """A stored record at `iterations`, in the writer's format, however few the config allows."""
    salt = bytes(range(16))
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(digest)}"


class _Spy:
    """Counts `hashlib.pbkdf2_hmac` calls and what iteration count each used."""

    def __init__(self, monkeypatch):
        self.calls: list[int] = []
        real = hashlib.pbkdf2_hmac

        def spy(name, password, salt, iterations):
            self.calls.append(iterations)
            return real(name, password, salt, iterations)

        monkeypatch.setattr(hashlib, "pbkdf2_hmac", spy)


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def __enter__(self):
        self._sink = logger.add(
            lambda m: self.lines.append((m.record["level"].name, m.record["message"]))
        )
        return self

    def __exit__(self, *exc):
        logger.remove(self._sink)

    def warnings(self) -> list[str]:
        return [text for level, text in self.lines if level == "WARNING"]


@pytest.mark.parametrize("iterations", [1, 10, 100, FLOOR - 1])
def test_a_record_below_the_floor_does_not_verify_even_for_the_right_password(iterations):
    assert _service().verify_password(PASSWORD, _record(PASSWORD, iterations)) is False


@pytest.mark.parametrize("iterations", [FLOOR, FLOOR + 1, CONFIGURED])
def test_CONTROL_a_record_at_or_above_the_floor_verifies(iterations):
    """The record is well formed, so the refusal above is about its count and nothing else."""
    assert _service().verify_password(PASSWORD, _record(PASSWORD, iterations)) is True


@pytest.mark.parametrize("iterations", [1, FLOOR - 1])
def test_the_refusal_names_the_records_count_and_the_floor(iterations):
    with _Log() as log:
        _service().verify_password(PASSWORD, _record(PASSWORD, iterations))

    (line,) = log.warnings()
    assert f"made at {iterations} PBKDF2 iterations" in line, line
    assert f"floor of {FLOOR}" in line, line
    assert PASSWORD not in line


def test_CONTROL_a_record_at_the_floor_logs_no_refusal():
    with _Log() as log:
        _service().verify_password(PASSWORD, _record(PASSWORD, FLOOR))

    assert log.warnings() == []


def test_a_refused_record_spends_one_pbkdf2_at_the_configured_count(monkeypatch):
    record = _record(PASSWORD, 1)  # made before the spy, which would count the making
    spy = _Spy(monkeypatch)

    _service().verify_password(PASSWORD, record)

    # The record's own count is never run: the cost is the configured one, as for an unknown name.
    assert spy.calls == [CONFIGURED]


def test_a_login_against_a_refused_record_costs_what_a_login_for_an_unknown_name_costs(monkeypatch):
    svc = _service()
    svc.create_user("alice", "alice@example.com", PASSWORD)
    svc._users["alice"]["password_hash"] = _record(PASSWORD, 100)
    assert svc.authenticate("nobody", "whatever-password") is None  # builds the stand-in hash
    spy = _Spy(monkeypatch)

    assert svc.authenticate("nobody", "whatever-password") is None
    unknown = list(spy.calls)
    spy.calls.clear()
    assert svc.authenticate("alice", PASSWORD) is None
    refused = list(spy.calls)

    assert unknown == refused == [CONFIGURED]


def test_a_refused_record_is_not_upgraded_by_the_login_it_failed():
    svc = _service()
    svc.create_user("alice", "alice@example.com", PASSWORD)
    weak = _record(PASSWORD, 100)
    svc._users["alice"]["password_hash"] = weak

    assert svc.authenticate("alice", PASSWORD) is None

    assert svc._users["alice"]["password_hash"] == weak


def test_the_login_failure_follows_the_refusal_warning():
    svc = _service()
    svc.create_user("alice", "alice@example.com", PASSWORD)
    svc._users["alice"]["password_hash"] = _record(PASSWORD, 100)

    with _Log() as log:
        svc.authenticate("alice", PASSWORD)

    warnings = log.warnings()
    assert len(warnings) == 2, warnings
    assert "below the floor" in warnings[0], warnings
    assert "invalid password for alice" in warnings[1], warnings


@pytest.mark.parametrize("iterations", [1, 10, FLOOR - 1, FLOOR])
def test_a_count_the_config_accepts_makes_records_verify_and_one_it_refuses_makes_none(iterations):
    """Both directions of the shared floor: the writer accepts exactly what the verifier will."""
    try:
        writer = _service(iterations)
    except ValidationError:
        assert _service().verify_password(PASSWORD, _record(PASSWORD, iterations)) is False
    else:
        assert writer.verify_password(PASSWORD, writer.hash_password(PASSWORD)) is True


def test_the_config_names_the_floor_when_it_refuses_a_count_below_it():
    with pytest.raises(ValidationError, match=str(FLOOR)):
        AuthConfig(secret_key=SECRET, pbkdf2_iterations=FLOOR - 1)


@pytest.mark.parametrize("count", [0, -1, -1000])
def test_a_record_claiming_no_iterations_is_malformed_not_below_the_floor(monkeypatch, count):
    """A count that is not a positive number is a corrupt record: refused without work and without
    a line saying it is below a floor, which would send an operator to the wrong cause."""
    record = f"pbkdf2_sha256${count}$c2FsdA$aGFzaA"
    spy = _Spy(monkeypatch)

    with _Log() as log:
        assert _service().verify_password(PASSWORD, record) is False

    assert spy.calls == []
    assert log.warnings() == []


def test_a_legacy_form_record_is_not_judged_by_the_floor():
    """The legacy hash is fixed at 100,000 iterations and carries no count to compare."""
    svc = _service()
    legacy = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), SECRET.encode()[:16], 100000).hex()

    assert svc.verify_password(PASSWORD, legacy) is True


def test_the_floor_is_the_documented_number_and_the_config_and_the_verifier_share_it():
    from cliffracer_auth.simple_auth import MIN_PBKDF2_ITERATIONS

    assert MIN_PBKDF2_ITERATIONS == FLOOR
    assert SimpleAuthService._MIN_ITERATIONS == MIN_PBKDF2_ITERATIONS
    (bound,) = [
        m.ge for m in AuthConfig.model_fields["pbkdf2_iterations"].metadata if hasattr(m, "ge")
    ]
    assert bound == MIN_PBKDF2_ITERATIONS
