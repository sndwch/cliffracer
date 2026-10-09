"""`AuthMiddleware` authenticates a request for the duration of its handler, and leaves no trace.

It is exported and was exercised nowhere: with the line that sets the context removed, every
test in the package still passed. These drive it with a stand-in request and a `call_next` that
records what a handler would see through `get_current_user()`.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cliffracer_auth import AuthMiddleware, get_current_user
from cliffracer_auth.simple_auth import (
    AuthConfig,
    AuthContext,
    AuthUser,
    SimpleAuthService,
    auth_context_var,
)

pytestmark = pytest.mark.unit

SECRET = "x" * 40


class _Request:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


@pytest.fixture
def svc():
    service = SimpleAuthService(AuthConfig(secret_key=SECRET))
    service.create_user("alice", "alice@example.com", "s3cret-password", roles={"admin"})
    return service


@pytest.fixture(autouse=True)
def _clean_context():
    token = auth_context_var.set(None)
    yield
    auth_context_var.reset(token)


def _spy():
    seen: list[AuthUser | None] = []

    async def call_next(request):
        seen.append(get_current_user())
        return "response"

    return seen, call_next


async def test_a_valid_bearer_token_authenticates_the_request_and_is_cleared_after(svc):
    token = svc.authenticate("alice", "s3cret-password")
    seen, call_next = _spy()

    response = await AuthMiddleware(svc)(_Request({"Authorization": f"Bearer {token}"}), call_next)

    assert response == "response"
    assert [user.username for user in seen if user] == ["alice"]
    assert get_current_user() is None


@pytest.mark.parametrize(
    "header_name, scheme",
    [("authorization", "bearer"), ("AUTHORIZATION", "BEARER"), ("Authorization", "bEaReR")],
)
async def test_the_header_name_and_the_scheme_are_matched_without_regard_to_case(
    svc, header_name, scheme
):
    token = svc.authenticate("alice", "s3cret-password")
    seen, call_next = _spy()

    await AuthMiddleware(svc)(_Request({header_name: f"{scheme} {token}"}), call_next)

    assert [user.username for user in seen if user] == ["alice"]


def _expired_token() -> str:
    now = datetime.now(UTC)
    claims = {
        "jti": "expired-jti",
        "user_id": "user_1",
        "username": "alice",
        "email": "alice@example.com",
        "exp": (now - timedelta(minutes=5)).timestamp(),
        "iat": (now - timedelta(hours=1)).timestamp(),
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic YWxpY2U6cHc="},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": f"Bearer {_expired_token()}"},
    ],
)
async def test_a_request_without_a_valid_token_proceeds_anonymously(svc, headers):
    seen, call_next = _spy()

    response = await AuthMiddleware(svc)(_Request(headers), call_next)

    assert response == "response", "the middleware does not refuse; the decorators do"
    assert seen == [None]


async def test_a_revoked_token_makes_an_anonymous_request(svc):
    token = svc.authenticate("alice", "s3cret-password")
    svc.revoke_token(token)
    seen, call_next = _spy()

    await AuthMiddleware(svc)(_Request({"Authorization": f"Bearer {token}"}), call_next)

    assert seen == [None]


def _outer_context() -> AuthContext:
    user = AuthUser(user_id="user_9", username="outer", email="o@example.com")
    return AuthContext(user=user, token="t", expires_at=datetime.now(UTC) + timedelta(hours=1))


@pytest.mark.parametrize("authenticated", [True, False])
async def test_the_context_set_before_the_request_is_restored_not_cleared(svc, authenticated):
    outer = _outer_context()
    auth_context_var.set(outer)
    token = svc.authenticate("alice", "s3cret-password") if authenticated else "garbage"
    _, call_next = _spy()

    await AuthMiddleware(svc)(_Request({"Authorization": f"Bearer {token}"}), call_next)

    assert auth_context_var.get() is outer


async def test_an_anonymous_request_does_not_inherit_the_identity_of_an_outer_context(svc):
    auth_context_var.set(_outer_context())
    seen, call_next = _spy()

    await AuthMiddleware(svc)(_Request({}), call_next)

    assert seen == [None], "the request ran as the outer user"


async def test_a_handler_that_raises_still_gets_the_context_restored_and_the_error_through(svc):
    outer = _outer_context()
    auth_context_var.set(outer)
    token = svc.authenticate("alice", "s3cret-password")

    async def call_next(request):
        assert get_current_user() is not None
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError, match="handler failed"):
        await AuthMiddleware(svc)(_Request({"Authorization": f"Bearer {token}"}), call_next)

    assert auth_context_var.get() is outer


async def test_CONTROL_the_spy_sees_the_context_a_handler_would_read(svc):
    """If the spy read nothing the tests above could not fail: it must see a set context."""
    context = svc.validate_token(svc.authenticate("alice", "s3cret-password"))
    assert context is not None and context.is_authenticated
    auth_context_var.set(context)
    seen, call_next = _spy()

    await call_next(_Request({}))

    assert seen[0] is not None and seen[0].username == "alice"
