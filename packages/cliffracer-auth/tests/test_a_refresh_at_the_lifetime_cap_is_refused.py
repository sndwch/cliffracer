"""A refresh at the instant the lifetime cap is reached is refused.

`refresh_max_lifetime_hours` is the time after which a token is no longer refreshed: once that many
hours have passed since the login that began the chain, a refresh is refused, as a JWT is refused
on or after its `exp`. One second before, it is granted. The clock is the module's own `datetime`,
replaced; the token is signed here with the service's key and an `oiat` it chooses.
"""

import time
from datetime import datetime

import jwt
import pytest
from cliffracer_auth import simple_auth
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

pytestmark = pytest.mark.unit

SECRET = "x" * 40


def _frozen_datetime(at: float) -> type:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(at, tz)

    return Frozen


def _refresh_at(monkeypatch, seconds_after_login: float) -> str | None:
    svc = SimpleAuthService(
        AuthConfig(secret_key=SECRET, pbkdf2_iterations=1000, refresh_max_lifetime_hours=1)
    )
    svc.create_user("alice", "alice@example.com", "s3cret-password")
    login = float(int(time.time()) - 10)
    claims = {
        "user_id": "user_1",
        "username": "alice",
        "email": "alice@example.com",
        "jti": "j1",
        "cid": "j1",
        "iat": time.time(),
        "exp": time.time() + 600,
        "oiat": login,
    }
    token = jwt.encode(claims, SECRET, algorithm="HS256")
    monkeypatch.setattr(simple_auth, "datetime", _frozen_datetime(login + seconds_after_login))
    return svc.refresh_token(token)


def test_a_refresh_at_the_instant_the_cap_is_reached_is_refused(monkeypatch):
    assert _refresh_at(monkeypatch, 3600.0) is None


def test_CONTROL_a_refresh_one_second_before_the_cap_is_granted(monkeypatch):
    assert _refresh_at(monkeypatch, 3599.0) is not None
