"""A user the service holds as inactive has no valid token, however recently it was issued.

`is_active` was enforced at login and at refresh only, so deactivating an account left every
token it already held valid until `exp` (a day by default), and the only way to cut access short
was to restart the process. Validation now reads the stored user.

The user a valid token yields is a projection of the token's claims, not the stored record.
"""

import time
from datetime import UTC, datetime

import jwt
import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

pytestmark = pytest.mark.unit

SECRET = "x" * 40
PASSWORD = "s3cret-password"


def _service() -> SimpleAuthService:
    svc = SimpleAuthService(AuthConfig(secret_key=SECRET))
    svc.create_user("alice", "alice@example.com", PASSWORD, roles={"admin"})
    svc.create_user("bobby", "bobby@example.com", PASSWORD)
    return svc


def test_the_token_of_a_deactivated_user_is_refused():
    svc = _service()
    token = svc.authenticate("alice", PASSWORD)
    assert token is not None and svc.validate_token(token) is not None

    svc._users["alice"]["user"].is_active = False

    assert svc.validate_token(token) is None


def test_CONTROL_deactivating_one_user_leaves_another_users_token_valid():
    svc = _service()
    alice = svc.authenticate("alice", PASSWORD)
    bobby = svc.authenticate("bobby", PASSWORD)
    assert alice is not None and bobby is not None

    svc._users["alice"]["user"].is_active = False

    assert svc.validate_token(alice) is None
    assert svc.validate_token(bobby) is not None


def test_reactivating_the_user_makes_the_same_token_valid_again():
    svc = _service()
    token = svc.authenticate("alice", PASSWORD)
    assert token is not None
    svc._users["alice"]["user"].is_active = False
    assert svc.validate_token(token) is None

    svc._users["alice"]["user"].is_active = True

    assert svc.validate_token(token) is not None


def test_a_token_for_a_user_this_service_has_no_record_of_is_accepted():
    """The store is in memory and a token may come from another issuer sharing the key, so a
    missing record is not a refusal; only a record that says inactive is."""
    svc = _service()
    token = svc.authenticate("alice", PASSWORD)
    assert token is not None
    del svc._users["alice"]

    assert svc.validate_token(token) is not None


def test_the_user_a_token_yields_is_a_projection_of_its_claims():
    svc = _service()
    stored = svc._users["alice"]["user"]
    stored.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    token = svc.authenticate("alice", PASSWORD)
    assert token is not None

    context = svc.validate_token(token)

    assert context is not None and context.user is not None
    user = context.user
    assert user is not stored
    assert (user.user_id, user.username, user.email) == (
        stored.user_id,
        stored.username,
        stored.email,
    )
    assert user.roles == {"admin"}
    assert user.is_active is True
    assert user.created_at > stored.created_at, "created_at is the validation time, not stored"


@pytest.mark.parametrize("spelling", ["alice", "Alice", "ALICE"])
def test_a_deactivated_users_token_is_refused_however_its_username_claim_is_cased(spelling):
    """The store is keyed by the lowered name, so the lookup has to canonicalise the claim the
    same way `create_user` does, or a token that spells the name differently is read as the
    token of a user with no record and accepted."""
    svc = _service()
    svc._users["alice"]["user"].is_active = False
    now = time.time()
    token = jwt.encode(
        {
            "jti": "a-jti",
            "user_id": "user_1",
            "username": spelling,
            "email": "alice@example.com",
            "roles": [],
            "permissions": [],
            "iat": now,
            "exp": now + 600,
        },
        SECRET,
        algorithm="HS256",
    )

    assert svc.validate_token(token) is None


@pytest.mark.parametrize("spelling", ["carol", "Carol", "CAROL"])
def test_CONTROL_the_same_three_spellings_of_a_user_with_no_record_are_accepted(spelling):
    svc = _service()
    now = time.time()
    token = jwt.encode(
        {
            "jti": "a-jti",
            "user_id": "user_9",
            "username": spelling,
            "email": "carol@example.com",
            "iat": now,
            "exp": now + 600,
        },
        SECRET,
        algorithm="HS256",
    )

    assert svc.validate_token(token) is not None
