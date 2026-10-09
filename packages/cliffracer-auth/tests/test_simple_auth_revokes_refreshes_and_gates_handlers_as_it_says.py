"""How `SimpleAuthService` revokes and refreshes, and how its decorators and middleware gate a caller.

A revoked token is refused by its jti after its chain revocation is forgotten. A revocation of a
token that has no jti revokes nothing. Revocations are dropped exactly at their expiry, and a token
expiring as it is revoked is not stored. A refresh past the lifetime cap is refused.
`requires_roles` and `requires_permissions` refuse an expired context, and no context, with
`AuthenticationError`, and the async `requires_permissions` refuses a caller without the
permission. A decorated async handler is still a coroutine function. The middleware reads only a
`Bearer` token.
"""

import asyncio
import time
import types
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cliffracer_auth import (
    AuthContext,
    AuthenticationError,
    AuthMiddleware,
    AuthorizationError,
    AuthUser,
    get_current_user,
    requires_auth,
    requires_permissions,
    requires_roles,
    simple_auth,
)
from cliffracer_auth.simple_auth import (
    AuthConfig,
    SimpleAuthService,
    auth_context_var,
    clear_current_context,
    set_current_context,
)

pytestmark = pytest.mark.unit

SECRET = "x" * 40


@pytest.fixture(autouse=True)
def _clean_context():
    reset = auth_context_var.set(None)
    yield
    auth_context_var.reset(reset)


def _service(**config) -> SimpleAuthService:
    return SimpleAuthService(AuthConfig(secret_key=SECRET, pbkdf2_iterations=1000, **config))


def _token(**claims) -> str:
    base = {"user_id": "user_1", "username": "alice", "email": "alice@example.com"}
    base.update(claims)
    return jwt.encode(base, SECRET, algorithm="HS256")


def _clock(monkeypatch, at: float) -> None:
    """Set the clock `revoke_token` reads."""
    monkeypatch.setattr(simple_auth, "time", types.SimpleNamespace(time=lambda: at))


# --- revocation ---------------------------------------------------------------------------------


def test_a_revoked_token_is_refused_after_its_chain_revocation_is_forgotten(monkeypatch):
    # A token from another issuer sharing the key can outlive this service's token lifetime, so
    # its chain revocation (one lifetime) is forgotten while its jti revocation (to its exp) holds.
    svc = _service(token_expiry_hours=1)
    now = time.time()
    long_lived = _token(jti="long", exp=now + 48 * 3600)
    _clock(monkeypatch, now)
    assert svc.revoke_token(long_lived) is True

    _clock(monkeypatch, now + 2 * 3600)  # past the chain's one-lifetime hold
    assert svc.revoke_token(_token(jti="other", exp=now + 48 * 3600)) is True
    assert "long" not in svc._revoked_chains, "the chain revocation is gone; only the jti remains"

    assert svc.validate_token(long_lived) is None


def test_a_chain_revocation_is_dropped_at_the_instant_its_hold_ends(monkeypatch):
    svc = _service(token_expiry_hours=1)
    now = time.time()
    _clock(monkeypatch, now)
    assert svc.revoke_token(_token(jti="a", cid="chain-c", exp=now + 600)) is True
    hold_ends = svc._revoked_chains["chain-c"]
    assert hold_ends >= now + 3600.0  # at least one token lifetime

    _clock(monkeypatch, hold_ends)  # the instant the hold ends
    assert svc.revoke_token(_token(jti="b", cid="chain-d", exp=hold_ends + 600)) is True

    assert "chain-c" not in svc._revoked_chains


def test_a_token_expiring_at_the_instant_it_is_revoked_is_not_stored(monkeypatch):
    svc = _service()
    now = float(int(time.time()))
    _clock(monkeypatch, now)

    assert svc.revoke_token(_token(jti="edge", exp=now)) is True

    assert svc._revoked_jtis == {}


def test_a_jti_revocation_is_dropped_at_the_instant_its_token_expires(monkeypatch):
    svc = _service()
    now = float(int(time.time()))
    _clock(monkeypatch, now)
    assert svc.revoke_token(_token(jti="a", exp=now + 100)) is True
    assert "a" in svc._revoked_jtis

    _clock(monkeypatch, now + 100)
    assert svc.revoke_token(_token(jti="b", exp=now + 1000)) is True

    assert "a" not in svc._revoked_jtis
    assert "b" in svc._revoked_jtis


@pytest.mark.parametrize("jti", [None, ""], ids=["absent", "empty"])
def test_a_token_with_a_chain_but_no_jti_is_not_revoked(jti):
    svc = _service()
    claims = {"cid": "chain-c", "exp": time.time() + 600}
    if jti is not None:
        claims["jti"] = jti

    assert svc.revoke_token(_token(**claims)) is False
    assert svc._revoked_chains == {}
    assert svc._revoked_jtis == {}


# --- refresh lifetime cap -----------------------------------------------------------------------


def _frozen_datetime(at: float) -> type:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(at, tz)

    return Frozen


def _refresh_at(monkeypatch, seconds_after_login: float) -> str | None:
    svc = _service(refresh_max_lifetime_hours=1)
    svc.create_user("alice", "alice@example.com", "s3cret-password")
    login = float(int(time.time()) - 10)
    token = _token(jti="j1", cid="j1", iat=time.time(), exp=time.time() + 600, oiat=login)
    monkeypatch.setattr(simple_auth, "datetime", _frozen_datetime(login + seconds_after_login))
    return svc.refresh_token(token)


def test_a_refresh_half_a_second_past_the_lifetime_cap_is_refused(monkeypatch):
    assert _refresh_at(monkeypatch, 3600.5) is None


def test_CONTROL_a_refresh_inside_the_lifetime_cap_is_granted(monkeypatch):
    assert _refresh_at(monkeypatch, 3599.0) is not None


# --- decorators ---------------------------------------------------------------------------------


def _set_context(*, expired: bool, roles=(), permissions=()) -> None:
    user = AuthUser(
        user_id="user_1",
        username="alice",
        email="alice@example.com",
        roles=set(roles),
        permissions=set(permissions),
    )
    delta = timedelta(hours=-1) if expired else timedelta(hours=1)
    set_current_context(AuthContext(user=user, token="t", expires_at=datetime.now(UTC) + delta))


def _decorated(decorator, *, is_async: bool):
    if is_async:

        async def handler():
            return "ran"

    else:

        def handler():
            return "ran"

    return decorator(handler)


def _call(func, is_async: bool):
    return asyncio.run(func()) if is_async else func()


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_requires_roles_refuses_an_expired_context_that_holds_the_role(is_async):
    func = _decorated(requires_roles("admin"), is_async=is_async)
    _set_context(expired=True, roles={"admin"})

    with pytest.raises(AuthenticationError):
        _call(func, is_async)


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_requires_permissions_refuses_an_expired_context_that_holds_the_permission(is_async):
    func = _decorated(requires_permissions("orders:read"), is_async=is_async)
    _set_context(expired=True, permissions={"orders:read"})

    with pytest.raises(AuthenticationError):
        _call(func, is_async)


@pytest.mark.parametrize(
    "decorator",
    [requires_roles("admin"), requires_permissions("orders:read")],
    ids=["roles", "permissions"],
)
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_the_role_decorators_refuse_no_context_with_authentication_error(decorator, is_async):
    clear_current_context()
    func = _decorated(decorator, is_async=is_async)

    with pytest.raises(AuthenticationError):
        _call(func, is_async)


def test_an_async_handler_without_the_permission_is_refused():
    func = _decorated(requires_permissions("orders:write"), is_async=True)
    _set_context(expired=False, permissions={"orders:read"})

    with pytest.raises(AuthorizationError):
        _call(func, True)


def test_CONTROL_an_async_handler_with_the_permission_runs():
    func = _decorated(requires_permissions("orders:write"), is_async=True)
    _set_context(expired=False, permissions={"orders:write"})

    assert _call(func, True) == "ran"


@pytest.mark.parametrize(
    "decorator",
    [requires_auth, requires_permissions("orders:read")],
    ids=["auth", "permissions"],
)
def test_an_async_handler_stays_a_coroutine_function_under_the_auth_decorators(decorator):
    # The timer dispatch awaits a method only when `asyncio.iscoroutinefunction` says so.
    func = _decorated(decorator, is_async=True)

    assert asyncio.iscoroutinefunction(func) is True


# --- middleware ---------------------------------------------------------------------------------


class _Request:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


@pytest.mark.parametrize("scheme", ["Basic", "Token"])
def test_the_middleware_ignores_a_valid_token_under_another_scheme(scheme):
    svc = _service()
    svc.create_user("alice", "alice@example.com", "s3cret-password")
    token = svc.authenticate("alice", "s3cret-password")
    assert token is not None
    seen = []

    async def call_next(request):
        seen.append(get_current_user())
        return "response"

    asyncio.run(AuthMiddleware(svc)(_Request({"Authorization": f"{scheme} {token}"}), call_next))

    assert seen == [None]


def test_CONTROL_the_middleware_accepts_the_same_token_as_bearer():
    svc = _service()
    svc.create_user("alice", "alice@example.com", "s3cret-password")
    token = svc.authenticate("alice", "s3cret-password")
    seen = []

    async def call_next(request):
        seen.append(get_current_user())
        return "response"

    asyncio.run(AuthMiddleware(svc)(_Request({"Authorization": f"Bearer {token}"}), call_next))

    assert [u.username for u in seen] == ["alice"]
