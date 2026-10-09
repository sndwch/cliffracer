"""`leeway_seconds` extends a token's life where the token is checked, not only where it is decoded.

The setting is documented as "an expired token is accepted for that long". `validate_token` decoded
with the leeway and then built the context with the token's own `exp`, so `AuthContext.is_valid` (what
`AuthExtension` and `requires_auth` read) was false for a token inside the window: the context came
back and the request was refused as `unauthenticated`, exactly as with no leeway at all.
"""

import time

import jwt
import pytest
from cliffracer_auth import AuthConfig, requires_auth
from cliffracer_auth.extension import AuthExtension
from cliffracer_auth.simple_auth import AuthContext, SimpleAuthService, auth_context_var

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit

SECRET = "test-secret-not-a-real-one-0123456789abcdef"


def _token(*, expired_for: float) -> str:
    """A token whose `exp` was `expired_for` seconds ago (negative: that many seconds ahead)."""
    now = time.time()
    return jwt.encode(
        {
            "jti": "j1",
            "cid": "j1",
            "user_id": "user_1",
            "username": "alice",
            "email": "a@x.io",
            "roles": [],
            "permissions": [],
            "exp": now - expired_for,
            "iat": now - 3600,
            "oiat": now - 3600,
        },
        SECRET,
        algorithm="HS256",
    )


def _service(leeway: float) -> SimpleAuthService:
    return SimpleAuthService(AuthConfig(secret_key=SECRET, leeway_seconds=leeway))


async def _rpc(auth: SimpleAuthService, token: str) -> dict:
    class Svc(CliffracerService):
        ext = AuthExtension(auth)

        @rpc
        async def whoami(self) -> str:
            return "reached the handler"

    async with ServiceTestHarness(Svc, config=ServiceConfig(name="who", health_port=0)) as harness:
        return (await harness.rpc("whoami", headers={"authorization": f"Bearer {token}"})).data


def test_a_token_expired_inside_the_leeway_is_an_authenticated_context():
    context = _service(30.0).validate_token(_token(expired_for=10))

    assert context is not None
    assert context.is_authenticated


def test_the_context_stays_valid_for_the_tokens_exp_plus_the_leeway():
    token = _token(expired_for=10)
    exp = jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})["exp"]

    context = _service(30.0).validate_token(token)

    assert context is not None and context.expires_at is not None
    assert context.expires_at.timestamp() == pytest.approx(exp + 30.0, abs=0.001)


async def test_a_request_with_a_token_inside_the_leeway_reaches_the_handler():
    reply = await _rpc(_service(30.0), _token(expired_for=10))

    assert reply.get("result") == "reached the handler", reply


async def test_requires_auth_admits_a_token_inside_the_leeway():
    auth = _service(30.0)

    @requires_auth
    async def guarded() -> str:
        return "admitted"

    context = auth.validate_token(_token(expired_for=10))
    reset = auth_context_var.set(context)
    try:
        assert await guarded() == "admitted"
    finally:
        auth_context_var.reset(reset)


async def test_CONTROL_with_no_leeway_a_token_expired_a_moment_ago_is_refused():
    reply = await _rpc(_service(0.0), _token(expired_for=1))

    assert reply["error"] == "refused: unauthenticated"


async def test_CONTROL_a_token_expired_past_the_leeway_is_refused():
    reply = await _rpc(_service(30.0), _token(expired_for=60))

    assert reply["error"] == "refused: unauthenticated"


async def test_CONTROL_a_token_that_has_not_expired_is_admitted_with_a_leeway():
    reply = await _rpc(_service(30.0), _token(expired_for=-600))

    assert reply.get("result") == "reached the handler"


def test_CONTROL_a_context_with_no_expiry_is_still_not_valid():
    assert AuthContext(user=None, token="t", expires_at=None).is_valid is False
