"""`validate_token` accepts a token only if its signature, algorithm and expiry hold.

Every other test in this package mints a token with the service itself and then
validates it, so a `validate_token` that stopped checking the signature or the
expiry would pass all of them. These build the tokens an attacker or a stale
client would hold, from the claims of a real one, and assert each is refused.
The first test is the other side of every refusal: without a token that does
validate, "returns None" would hold for a service that refused everything.
"""

import json
import time
from unittest.mock import AsyncMock

import jwt
import pytest
from cliffracer_auth import AuthConfig, AuthExtension, SimpleAuthService

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit

# Long enough for HS512 (64 bytes), so signing a token with a different
# algorithm than the service's own raises no key-length warning.
SECRET = "test-secret-not-a-real-one-0123456789abcdef-0123456789abcdef-0123456789"
OTHER_SECRET = "a-different-secret-not-the-service-s-0123456789abcdef-0123456789abcdef"


def _service() -> SimpleAuthService:
    auth = SimpleAuthService(AuthConfig(secret_key=SECRET))
    auth.create_user(
        username="ana", email="ana@example.invalid", password="pw-ana-12345", roles={"user"}
    )
    return auth


def _claims(auth: SimpleAuthService) -> dict:
    """The claims of a token this service minted, to be re-signed by someone else."""
    token = auth.authenticate("ana", "pw-ana-12345")
    assert token, "the fixture user must be able to authenticate"
    return jwt.decode(token, SECRET, algorithms=[auth.config.algorithm])


def test_a_token_the_service_minted_validates():
    auth = _service()
    token = auth.authenticate("ana", "pw-ana-12345")
    assert token is not None

    context = auth.validate_token(token)

    assert context is not None
    assert context.user.username == "ana"


def test_a_token_signed_with_another_secret_is_refused():
    auth = _service()
    forged = jwt.encode(_claims(auth), OTHER_SECRET, algorithm=auth.config.algorithm)

    assert auth.validate_token(forged) is None


def test_an_expired_token_signed_with_the_right_secret_is_refused():
    auth = _service()
    claims = _claims(auth) | {"exp": time.time() - 10}
    expired = jwt.encode(claims, SECRET, algorithm=auth.config.algorithm)

    assert auth.validate_token(expired) is None


def test_a_token_signed_with_a_different_algorithm_is_refused():
    auth = _service()
    assert auth.config.algorithm != "HS512"
    other_algorithm = jwt.encode(_claims(auth), SECRET, algorithm="HS512")

    assert auth.validate_token(other_algorithm) is None


def test_an_unsigned_token_is_refused():
    auth = _service()
    unsigned = jwt.encode(_claims(auth), key=None, algorithm="none")

    assert auth.validate_token(unsigned) is None


def test_a_forged_or_expired_token_is_not_refreshed_into_a_valid_one():
    auth = _service()
    claims = _claims(auth)
    forged = jwt.encode(claims, OTHER_SECRET, algorithm=auth.config.algorithm)
    expired = jwt.encode(
        claims | {"exp": time.time() - 10}, SECRET, algorithm=auth.config.algorithm
    )

    assert auth.refresh_token(forged) is None
    assert auth.refresh_token(expired) is None


def _msg(subject, data, headers):
    m = AsyncMock()
    m.subject = subject
    m.data = json.dumps(data).encode()
    m.headers = headers
    return m


@pytest.mark.parametrize("kind", ["forged", "expired"])
async def test_the_extension_refuses_a_forged_or_expired_token_before_the_handler(kind):
    auth = _service()

    class Svc(CliffracerService):
        auth_ext = AuthExtension(auth)

        @rpc
        async def whoami(self) -> str:
            return "reached"

    svc = Svc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    claims = _claims(auth)
    if kind == "forged":
        token = jwt.encode(claims, OTHER_SECRET, algorithm=auth.config.algorithm)
    else:
        token = jwt.encode(
            claims | {"exp": time.time() - 10}, SECRET, algorithm=auth.config.algorithm
        )

    msg = _msg("s.rpc.whoami", {}, {"authorization": f"Bearer {token}"})
    await svc.container._handle_rpc_request(msg)

    replies = [json.loads(c.args[0].decode()) for c in msg.respond.await_args_list]
    assert replies, "a refused call must still get an answer"
    assert "unauthenticated" in json.dumps(replies[0]), replies[0]
    assert replies[0].get("result") != "reached", replies[0]
