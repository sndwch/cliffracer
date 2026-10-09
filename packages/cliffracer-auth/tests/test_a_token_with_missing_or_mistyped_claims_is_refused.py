"""`validate_token` returns None for anything that is not a valid token, whatever its payload.

A correctly signed token is not necessarily a usable one: PyJWT does not require `exp`, `user_id`,
`username` or `email`, and a `roles` claim that is not a list cannot be iterated. Callers that
call `validate_token` directly (a web framework's dependency, a WebSocket handshake) read None as
"refuse", so an exception from it is a 500 where a 401 was meant.
"""

import time

import jwt
import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

pytestmark = pytest.mark.unit

SECRET = "x" * 40


def _service() -> SimpleAuthService:
    return SimpleAuthService(AuthConfig(secret_key=SECRET))


def _claims(**overrides):
    now = time.time()
    claims = {
        "jti": "a-jti",
        "user_id": "user_9",
        "username": "mallory",
        "email": "mallory@example.com",
        "roles": ["user"],
        "permissions": ["orders:read"],
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not _MISSING}


_MISSING = object()


def _sign(**overrides) -> str:
    return jwt.encode(_claims(**overrides), SECRET, algorithm="HS256")


def test_CONTROL_a_well_formed_token_from_another_issuer_is_accepted():
    context = _service().validate_token(_sign())

    assert context is not None
    assert context.user is not None
    assert (context.user.user_id, context.user.username) == ("user_9", "mallory")
    assert context.user.roles == {"user"}
    assert context.user.permissions == {"orders:read"}


@pytest.mark.parametrize("claim", ["exp", "jti", "user_id", "username", "email"])
def test_a_token_missing_a_required_claim_is_refused(claim):
    assert _service().validate_token(_sign(**{claim: _MISSING})) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"roles": 5},
        {"roles": "admin"},
        {"roles": ["user", 7]},
        {"permissions": {"a": 1}},
        {"permissions": [None]},
        {"user_id": 9},
        {"username": ["mallory"]},
        {"email": None},
        {"email": 7},
        {"exp": "soon"},
        {"exp": 1e30},
        {"jti": 12},
    ],
    ids=lambda overrides: ",".join(f"{k}={v!r}" for k, v in overrides.items()),
)
def test_a_token_with_a_claim_of_the_wrong_type_is_refused(overrides):
    assert _service().validate_token(_sign(**overrides)) is None


def test_absent_roles_and_permissions_are_empty_not_an_error():
    context = _service().validate_token(_sign(roles=_MISSING, permissions=_MISSING))

    assert context is not None
    assert context.user is not None
    assert context.user.roles == set() and context.user.permissions == set()
