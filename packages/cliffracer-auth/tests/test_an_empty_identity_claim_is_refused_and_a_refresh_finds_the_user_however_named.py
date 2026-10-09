"""A token with an empty or blank identity claim is not a token of this service, and a refresh
looks its user up the way every other method does.

`create_user` takes a username of three or more letters, digits, `_`, `-` or `.`, an email with an
`@` and a `.` after it, and generates the user id, so a token this service minted never carries a
`user_id`, `username` or `email` that is empty or whitespace alone; `validate_token` refuses one
that does. A token whose `username` claim is "ALICE" names the stored user "alice", as it does for
`validate_token`, so `refresh_token` re-issues it from that user.
"""

import time

import jwt
import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

pytestmark = pytest.mark.unit

SECRET = "x" * 40


def _service() -> SimpleAuthService:
    return SimpleAuthService(AuthConfig(secret_key=SECRET))


def _sign(**overrides) -> str:
    now = time.time()
    claims = {
        "jti": "a-jti",
        "user_id": "user_9",
        "username": "mallory",
        "email": "mallory@example.com",
        "iat": now,
        "oiat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, SECRET, algorithm="HS256")


@pytest.mark.parametrize(
    "blank", ["", " ", "\t\n", "\u3000"], ids=["empty", "space", "tab-newline", "ideographic-space"]
)
@pytest.mark.parametrize("claim", ["user_id", "username", "email"])
def test_a_token_with_an_empty_or_blank_identity_claim_is_refused(claim, blank):
    assert _service().validate_token(_sign(**{claim: blank})) is None


def test_CONTROL_a_claim_with_whitespace_around_a_name_is_not_blank():
    """Only a claim that is whitespace alone is refused; what is accepted is not stripped."""
    context = _service().validate_token(_sign(email=" mallory@example.com "))

    assert context is not None and context.user is not None
    assert context.user.email == " mallory@example.com "


def test_CONTROL_the_same_token_with_every_identity_claim_set_is_accepted():
    """Without this, the refusal above could be a refusal of every token from another issuer."""
    assert _service().validate_token(_sign()) is not None


@pytest.mark.parametrize("spelling", ["ALICE", "Alice", "alice"])
def test_a_refresh_finds_the_stored_user_however_the_token_spells_the_name(spelling):
    service = _service()
    alice = service.create_user("alice", "alice@example.com", "pass12345", roles={"ops"})

    refreshed = service.refresh_token(_sign(user_id=alice.user_id, username=spelling))

    assert refreshed is not None
    claims = jwt.decode(refreshed, SECRET, algorithms=["HS256"])
    assert (claims["username"], claims["roles"]) == ("alice", ["ops"])


def test_CONTROL_a_refresh_for_a_name_the_service_does_not_hold_is_refused():
    """Without this, the refresh above could re-issue any token it is given."""
    service = _service()
    service.create_user("alice", "alice@example.com", "pass12345")

    assert service.refresh_token(_sign(username="bob")) is None
