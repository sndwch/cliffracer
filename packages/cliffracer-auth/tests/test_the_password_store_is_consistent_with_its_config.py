"""The password store agrees with its own configuration.

- `pbkdf2_iterations` is bounded to what `verify_password` will accept, below and above, so a
  configuration that creates users it would then refuse to verify fails at construction instead of
  at every login.
- A user that does not exist costs the same PBKDF2 as one that does, so response time does not
  say which usernames are registered. Timing is flaky to assert; the work done is not, so the
  tests count the PBKDF2 calls a login makes.
- A user id is never reused while the process lives, even after a record is removed.
- A record made at fewer iterations than the service is now configured for still verifies, and
  is written again at today's cost at the next successful login.
"""

import hashlib

import pytest
from cliffracer_auth.simple_auth import MAX_PBKDF2_ITERATIONS, AuthConfig, SimpleAuthService
from pydantic import ValidationError

pytestmark = pytest.mark.unit

SECRET = "x" * 40
PASSWORD = "s3cret-password"


def _service(iterations: int = 1000) -> SimpleAuthService:
    return SimpleAuthService(AuthConfig(secret_key=SECRET, pbkdf2_iterations=iterations))


@pytest.mark.parametrize("iterations", [0, -1, 1, 999, MAX_PBKDF2_ITERATIONS + 1])
def test_an_iteration_count_verify_password_would_refuse_is_refused_at_construction(iterations):
    with pytest.raises(ValidationError, match="pbkdf2_iterations"):
        AuthConfig(secret_key=SECRET, pbkdf2_iterations=iterations)


@pytest.mark.parametrize("iterations", [1_000, MAX_PBKDF2_ITERATIONS])
def test_CONTROL_the_bounds_themselves_are_accepted(iterations):
    assert (
        AuthConfig(secret_key=SECRET, pbkdf2_iterations=iterations).pbkdf2_iterations == iterations
    )


def test_the_ceiling_the_config_allows_is_the_one_verify_password_enforces():
    assert SimpleAuthService._MAX_ITERATIONS == MAX_PBKDF2_ITERATIONS


def test_the_floor_the_config_allows_is_the_one_verify_password_enforces():
    from cliffracer_auth.simple_auth import MIN_PBKDF2_ITERATIONS

    assert SimpleAuthService._MIN_ITERATIONS == MIN_PBKDF2_ITERATIONS == 1_000


class _Spy:
    """Counts `hashlib.pbkdf2_hmac` calls and what iteration count each used."""

    def __init__(self, monkeypatch):
        self.calls: list[int] = []
        real = hashlib.pbkdf2_hmac

        def spy(name, password, salt, iterations):
            self.calls.append(iterations)
            return real(name, password, salt, iterations)

        monkeypatch.setattr(hashlib, "pbkdf2_hmac", spy)


def test_a_login_for_an_unknown_user_does_one_pbkdf2_like_a_known_user_does(monkeypatch):
    svc = _service(1000)
    svc.create_user("alice", "alice@example.com", PASSWORD)
    assert svc.authenticate("nobody", "whatever-password") is None  # builds the stand-in hash
    spy = _Spy(monkeypatch)

    assert svc.authenticate("nobody", "whatever-password") is None
    unknown = list(spy.calls)
    spy.calls.clear()
    assert svc.authenticate("alice", "wrong-password-here") is None
    wrong_password = list(spy.calls)
    spy.calls.clear()
    assert svc.authenticate("alice", PASSWORD) is not None
    right_password = list(spy.calls)

    assert unknown == wrong_password == right_password == [1000]


def test_user_ids_are_not_reused_after_a_record_is_removed():
    svc = _service()
    for name in ("aaa", "bbb"):
        svc.create_user(name, f"{name}@example.com", PASSWORD)
    del svc._users["aaa"]

    svc.create_user("ccc", "ccc@example.com", PASSWORD)

    ids = [data["user"].user_id for data in svc._users.values()]
    assert len(ids) == len(set(ids)) == 2, ids


def test_CONTROL_the_first_users_are_numbered_from_one():
    svc = _service()
    first = svc.create_user("aaa", "aaa@example.com", PASSWORD)
    second = svc.create_user("bbb", "bbb@example.com", PASSWORD)

    assert (first.user_id, second.user_id) == ("user_1", "user_2")


def _weaker_record(svc: SimpleAuthService, username: str, iterations: int) -> str:
    """The stored record of `username`, made at `iterations` by a service configured lower."""
    weaker = _service(iterations)
    record = weaker.hash_password(PASSWORD)
    svc._users[username]["password_hash"] = record
    return record


def test_a_record_below_the_configured_cost_verifies_and_is_upgraded_at_login():
    svc = _service(2000)
    svc.create_user("alice", "alice@example.com", PASSWORD)
    weak = _weaker_record(svc, "alice", 1000)
    assert svc.verify_password(PASSWORD, weak)

    assert svc.authenticate("alice", PASSWORD) is not None

    upgraded = svc._users["alice"]["password_hash"]
    assert upgraded != weak and upgraded.split("$")[1] == "2000"
    assert svc.verify_password(PASSWORD, upgraded)


def test_a_failed_login_does_not_upgrade_a_weak_record():
    svc = _service(2000)
    svc.create_user("alice", "alice@example.com", PASSWORD)
    weak = _weaker_record(svc, "alice", 1000)

    assert svc.authenticate("alice", "not-the-password") is None

    assert svc._users["alice"]["password_hash"] == weak


def test_CONTROL_a_record_at_the_configured_cost_is_not_written_again():
    svc = _service(2000)
    svc.create_user("alice", "alice@example.com", PASSWORD)
    before = svc._users["alice"]["password_hash"]

    assert svc.authenticate("alice", PASSWORD) is not None
    assert svc.authenticate("alice", PASSWORD) is not None

    assert svc._users["alice"]["password_hash"] == before
