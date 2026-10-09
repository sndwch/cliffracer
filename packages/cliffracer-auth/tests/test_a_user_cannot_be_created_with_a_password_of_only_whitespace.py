"""`create_user` refuses a password of whitespace alone, and a refused call leaves nothing behind.

`create_user` is the one place a password is validated, and it accepted eight spaces: the user was
stored and could log in with them. It now raises `ValidationError` before anything is stored, so
no user exists, no user id is used up, and the same name can be created afterwards with a real one.
"""

import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

from cliffracer.core.validation import ValidationError

pytestmark = pytest.mark.unit

SECRET = "x" * 40


def _service() -> SimpleAuthService:
    return SimpleAuthService(AuthConfig(secret_key=SECRET))


@pytest.mark.parametrize(
    "password", [" " * 8, "\t" * 8, "\xa0" * 12], ids=["spaces", "tabs", "nbsp"]
)
def test_a_user_cannot_be_created_with_a_password_of_whitespace_alone(password):
    svc = _service()

    with pytest.raises(ValidationError, match="not whitespace"):
        svc.create_user("alice", "alice@example.com", password)

    assert svc._users == {}
    assert svc.authenticate("alice", password) is None


def test_a_refused_call_does_not_use_up_a_user_id_or_the_name():
    svc = _service()
    with pytest.raises(ValidationError):
        svc.create_user("alice", "alice@example.com", " " * 8)

    created = svc.create_user("alice", "alice@example.com", "a-real-password")

    assert created.user_id == "user_1"


def test_CONTROL_a_password_with_whitespace_around_a_character_is_kept_exactly_and_logs_in():
    svc = _service()
    svc.create_user("alice", "alice@example.com", " a      ")

    assert svc.authenticate("alice", " a      ") is not None
    assert svc.authenticate("alice", "a") is None
