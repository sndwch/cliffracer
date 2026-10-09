"""The revoked set holds only tokens that could still be presented.

Once a token's `exp` has passed it is refused on its own, so keeping its jti is memory growing
for the life of the process for nothing. A revocation is dropped when its token expires, and an
already-expired token is not stored at all.
"""

import time

import jwt
import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

pytestmark = pytest.mark.unit

SECRET = "x" * 40


def _token(jti: str, *, lives: float) -> str:
    now = time.time()
    claims = {
        "jti": jti,
        "user_id": "user_1",
        "username": "alice",
        "email": "alice@example.com",
        "iat": now - 10,
        "exp": now + lives,
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _service() -> SimpleAuthService:
    return SimpleAuthService(AuthConfig(secret_key=SECRET))


def test_an_already_expired_token_is_not_stored():
    svc = _service()

    for number in range(20):
        assert svc.revoke_token(_token(f"old{number}", lives=-60)) is True

    assert svc._revoked_jtis == {}


def test_a_revocation_is_dropped_once_its_token_has_expired():
    svc = _service()
    short = _token("short-lived", lives=1.0)
    assert svc.revoke_token(short) is True
    assert "short-lived" in svc._revoked_jtis
    assert svc.validate_token(short) is None

    time.sleep(1.2)  # the token's own expiry passes
    assert svc.revoke_token(_token("later", lives=600)) is True

    assert "short-lived" not in svc._revoked_jtis
    assert "later" in svc._revoked_jtis
    assert svc.validate_token(short) is None, "an expired token is still refused, on its expiry"


def test_CONTROL_a_revocation_whose_token_is_still_live_is_kept():
    svc = _service()
    live = _token("still-live", lives=600)
    assert svc.revoke_token(live) is True

    assert svc.revoke_token(_token("another", lives=600)) is True

    assert set(svc._revoked_jtis) == {"still-live", "another"}
    assert svc.validate_token(live) is None


def test_a_token_with_no_usable_exp_cannot_be_revoked():
    svc = _service()
    claims = {"jti": "no-exp", "user_id": "u", "username": "a", "email": "a@example.com"}

    assert svc.revoke_token(jwt.encode(claims, SECRET, algorithm="HS256")) is False
    assert svc._revoked_jtis == {}
