"""The JWT signing secret does not appear in a printed, logged or dumped `AuthConfig`.

`secret_key` was a plain `str`, so `repr(config)`, an f-string of it, a structured log line that
carried it and `model_dump()` all printed the key that signs every token. It is a `SecretStr` now.
The tests that follow also read the other direction: the key still signs, verifies, revokes and
salts a legacy hash, each checked against PyJWT or hashlib using the key itself, so a service that
signed with the mask would not pass.
"""

import hashlib
import io
import time

import jwt
import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService
from loguru import logger
from pydantic import SecretStr

pytestmark = pytest.mark.unit

SECRET = "the-signing-key-" + "k" * 24


def _service(secret: str | SecretStr = SECRET) -> SimpleAuthService:
    return SimpleAuthService(AuthConfig(secret_key=secret))


def _logged(message: str) -> str:
    sink = io.StringIO()
    handler = logger.add(sink, format="{message}")
    try:
        logger.info(message)
    finally:
        logger.remove(handler)
    return sink.getvalue()


# ---- the secret is not printed ---------------------------------------------------------------


def test_the_repr_and_the_str_of_a_config_do_not_contain_the_secret():
    config = AuthConfig(secret_key=SECRET)

    assert SECRET not in repr(config)
    assert SECRET not in str(config)
    assert SECRET not in f"{config}"
    assert "secret_key=SecretStr('**********')" in repr(config)


def test_a_service_whose_config_is_printed_does_not_print_the_secret():
    svc = _service()

    assert SECRET not in repr(svc.config)
    assert SECRET not in _logged(f"starting with {svc.config}")


def test_a_dump_of_the_config_does_not_contain_the_secret():
    config = AuthConfig(secret_key=SECRET)

    assert SECRET not in str(config.model_dump())
    assert SECRET not in str(config.model_dump(mode="json"))
    assert SECRET not in config.model_dump_json()


def test_the_other_fields_still_print():
    config = AuthConfig(secret_key=SECRET, token_expiry_hours=7)

    assert "token_expiry_hours=7" in repr(config)
    assert "algorithm='HS256'" in repr(config)


# ---- the secret still does its work ----------------------------------------------------------


def test_a_token_is_signed_with_the_secret_itself():
    svc = _service()
    svc.create_user("alice", "alice@example.com", "a-long-enough-password")

    token = svc.authenticate("alice", "a-long-enough-password")

    assert token is not None
    assert jwt.decode(token, SECRET, algorithms=["HS256"])["username"] == "alice"


def test_a_token_signed_elsewhere_with_the_secret_validates():
    """A host sharing the key mints with no longer a lifetime than this service's."""
    svc = _service()
    now = int(time.time())
    claims = {
        "jti": "j1",
        "user_id": "u1",
        "username": "bob",
        "email": "bob@example.com",
        "iat": now,
        "exp": now + 3600,
    }

    context = svc.validate_token(jwt.encode(claims, SECRET, algorithm="HS256"))

    assert context is not None and context.user is not None
    assert context.user.username == "bob"


def test_a_token_signed_with_another_secret_is_refused():
    svc = _service()
    claims = {
        "jti": "j1",
        "user_id": "u1",
        "username": "bob",
        "email": "bob@example.com",
        "exp": 4_102_444_800,
    }

    assert svc.validate_token(jwt.encode(claims, "z" * 40, algorithm="HS256")) is None
    # A token signed with the mask itself is not the secret's token either.
    assert svc.validate_token(jwt.encode(claims, "**********", algorithm="HS256")) is None


def test_revoking_and_refreshing_read_the_secret_not_its_mask():
    svc = _service()
    svc.create_user("alice", "alice@example.com", "a-long-enough-password")
    token = svc.authenticate("alice", "a-long-enough-password")
    assert token is not None

    refreshed = svc.refresh_token(token)
    assert refreshed is not None
    assert jwt.decode(refreshed, SECRET, algorithms=["HS256"])["username"] == "alice"

    assert svc.revoke_token(refreshed) is True
    assert svc.validate_token(refreshed) is None


def test_a_legacy_hash_is_salted_with_the_secret_itself():
    svc = _service()
    legacy = hashlib.pbkdf2_hmac("sha256", b"old-password", SECRET.encode()[:16], 100000).hex()

    assert svc.verify_password("old-password", legacy)


def test_a_secret_given_as_a_secretstr_works_the_same():
    svc = _service(SecretStr(SECRET))
    svc.create_user("alice", "alice@example.com", "a-long-enough-password")
    token = svc.authenticate("alice", "a-long-enough-password")

    assert token is not None
    assert jwt.decode(token, SECRET, algorithms=["HS256"])["username"] == "alice"


def test_a_secret_assigned_later_is_a_secret_too_and_signs():
    """Rotating the key on a live config by assignment keeps working, and stays masked."""
    svc = _service()
    rotated = "r" * 40

    svc.config.secret_key = rotated  # type: ignore[assignment]  # a str is coerced on assignment
    svc.create_user("alice", "alice@example.com", "a-long-enough-password")
    token = svc.authenticate("alice", "a-long-enough-password")

    assert token is not None
    assert jwt.decode(token, rotated, algorithms=["HS256"])["username"] == "alice"
    assert rotated not in repr(svc.config)


def test_a_secret_shorter_than_the_minimum_is_still_refused():
    with pytest.raises(ValueError, match="at least 32 characters"):
        SimpleAuthService(AuthConfig(secret_key="short"))
