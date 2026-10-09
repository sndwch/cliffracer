"""What `SimpleAuthService` and `AuthConfig` refuse at their bounds.

The PBKDF2 iteration count has a ceiling. An email must hold an `@` and a dotted domain, is refused
by `ValueError` without one, and is between 3 and 254 characters. A context is invalid at the
instant it expires. A password record with an empty salt is refused even when its digest matches,
a salt that needs padding verifies, and the legacy path answers `False` on an encode error. A token
with an empty `jti` is refused. A salt and a token id each carry at least 16 random bytes.
"""

import base64
import hashlib
import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cliffracer_auth import simple_auth
from cliffracer_auth.simple_auth import AuthConfig, AuthContext, SimpleAuthService
from pydantic import ValidationError

pytestmark = pytest.mark.unit

SECRET = "x" * 40


def _service(**overrides):
    return SimpleAuthService(AuthConfig(secret_key=SECRET, pbkdf2_iterations=1000, **overrides))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_the_config_refuses_an_iteration_count_one_above_the_ceiling():
    assert AuthConfig(secret_key=SECRET, pbkdf2_iterations=10_000_000).pbkdf2_iterations == (
        10_000_000
    )
    with pytest.raises(ValidationError):
        AuthConfig(secret_key=SECRET, pbkdf2_iterations=10_000_001)


def test_an_email_without_a_dot_after_its_at_sign_is_refused():
    svc = _service()
    with pytest.raises(ValueError, match="Invalid email format"):
        svc.create_user("alice", "a@bc", "a-good-password-1")
    assert "alice" not in svc._users


def test_an_email_without_an_at_sign_is_refused_as_a_bad_format():
    svc = _service()
    with pytest.raises(ValueError, match="Invalid email format"):
        svc.create_user("alice", "abcd.ef", "a-good-password-1")


def test_an_email_of_three_characters_is_long_enough():
    svc = _service()
    user = svc.create_user("alice", "a@.", "a-good-password-1")
    assert user.email == "a@."


def test_an_email_of_255_characters_is_refused_and_254_accepted():
    svc = _service()
    domain = "@example.com"
    ok = "a" * (254 - len(domain)) + domain
    assert len(ok) == 254
    assert svc.create_user("alice", ok, "a-good-password-1").email == ok
    too_long = "a" * (255 - len(domain)) + domain
    with pytest.raises(ValueError):
        svc.create_user("bob", too_long, "a-good-password-1")
    assert "bob" not in svc._users


def test_a_context_is_no_longer_valid_at_the_instant_it_expires(monkeypatch):
    fixed = datetime(2030, 1, 1, tzinfo=UTC)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(simple_auth, "datetime", Frozen)
    assert AuthContext(expires_at=fixed + timedelta(microseconds=1)).is_valid is True
    assert AuthContext(expires_at=fixed).is_valid is False


def test_a_record_with_an_empty_salt_is_refused_even_with_a_matching_digest():
    svc = _service()
    digest = hashlib.pbkdf2_hmac("sha256", b"pw", b"", 1000)
    record = f"pbkdf2_sha256$1000$${_b64(digest)}"
    assert svc.verify_password("pw", record) is False


def test_a_record_whose_salt_encodes_to_a_length_needing_two_pad_characters_verifies():
    svc = _service()
    salt = b"seven!!"  # 7 bytes -> 10 base64 characters, 2 padding characters stripped
    assert len(_b64(salt)) == 10
    digest = hashlib.pbkdf2_hmac("sha256", b"pw", salt, 1000)
    record = f"pbkdf2_sha256$1000${_b64(salt)}${_b64(digest)}"
    assert svc.verify_password("pw", record) is True
    assert svc.verify_password("other", record) is False


def test_a_legacy_record_that_cannot_be_encoded_is_refused_with_false():
    svc = _service()
    # A lone surrogate cannot be UTF-8 encoded; a JSON request can carry one.
    assert svc.verify_password("\udc80", "deadbeef") is False
    assert svc.verify_password("pw", "dead\udc80beef") is False


def test_a_token_with_an_empty_jti_is_refused_even_when_it_names_a_chain():
    svc = _service()
    now = time.time()
    token = jwt.encode(
        {
            "jti": "",
            "cid": "chain-1",
            "user_id": "user_1",
            "username": "alice",
            "email": "alice@example.com",
            "exp": now + 3600,
            "iat": now,
        },
        SECRET,
        algorithm="HS256",
    )
    # Control: the same token with a jti is accepted, so the refusal is the jti's.
    good = jwt.encode(
        {**jwt.decode(token, SECRET, algorithms=["HS256"]), "jti": "abc"}, SECRET, "HS256"
    )
    assert svc.validate_token(good) is not None
    assert svc.validate_token(token) is None


def test_a_password_record_carries_at_least_sixteen_bytes_of_salt():
    """A floor, not the exact length: a longer salt is as good, a shorter one is weaker."""
    record = _service().hash_password("s3cret-password")
    salt = record.split("$")[2]

    assert len(base64.urlsafe_b64decode(salt + "=" * (-len(salt) % 4))) >= 16


def test_a_token_id_carries_at_least_sixteen_random_bytes():
    """A floor, not the exact length: a jti is what a revocation names, so a short one collides."""
    service = _service()
    service.create_user("alice", "alice@example.com", "s3cret-password")
    token = service.authenticate("alice", "s3cret-password")

    jti = jwt.decode(token, options={"verify_signature": False})["jti"]

    assert len(bytes.fromhex(jti)) >= 16
