"""The three auth decorators, which the docs called removed and which had no tests.

They are real, and exported from cliffracer.auth. They read the auth context a
request carries: `AuthExtension.worker_setup` sets it on a NATS dispatch (the
dispatch path is exercised in test_auth_context_propagation.py) and
`AuthMiddleware` sets it on HTTP. With none set, a decorated handler raises
AuthenticationError, which is what the tests here read.
"""

from datetime import UTC, datetime, timedelta

import pytest
from cliffracer_auth import (
    AuthContext,
    AuthenticationError,
    AuthorizationError,
    AuthUser,
    requires_auth,
    requires_permissions,
    requires_roles,
)
from cliffracer_auth.simple_auth import clear_current_context, set_current_context

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_context():
    clear_current_context()
    yield
    clear_current_context()


def _authenticate(roles=None, permissions=None):
    user = AuthUser(
        user_id="user_1",
        username="alice",
        email="alice@example.com",
        roles=roles or set(),
        permissions=permissions or set(),
    )
    set_current_context(
        AuthContext(
            user=user,
            token="t",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )


class TestRequiresAuth:
    def test_sync_call_allowed_with_a_context(self):
        @requires_auth
        def handler():
            return "ok"

        _authenticate()
        assert handler() == "ok"

    def test_sync_call_refused_without_a_context(self):
        @requires_auth
        def handler():
            return "ok"

        with pytest.raises(AuthenticationError):
            handler()

    @pytest.mark.asyncio
    async def test_async_call_allowed_with_a_context(self):
        @requires_auth
        async def handler():
            return "ok"

        _authenticate()
        assert await handler() == "ok"

    @pytest.mark.asyncio
    async def test_async_call_refused_without_a_context(self):
        @requires_auth
        async def handler():
            return "ok"

        with pytest.raises(AuthenticationError):
            await handler()

    def test_an_expired_context_is_not_authenticated(self):
        @requires_auth
        def handler():
            return "ok"

        set_current_context(
            AuthContext(
                user=AuthUser(user_id="u", username="a", email="a@b.co"),
                token="t",
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        with pytest.raises(AuthenticationError):
            handler()


class TestRequiresRoles:
    def test_allowed_on_a_matching_role(self):
        @requires_roles("admin")
        def handler():
            return "ok"

        _authenticate(roles={"admin"})
        assert handler() == "ok"

    def test_refused_without_the_role(self):
        @requires_roles("admin")
        def handler():
            return "ok"

        _authenticate(roles={"user"})
        with pytest.raises(AuthorizationError):
            handler()

    def test_any_of_several_roles_is_enough(self):
        @requires_roles("admin", "operator")
        def handler():
            return "ok"

        _authenticate(roles={"operator"})
        assert handler() == "ok"

    @pytest.mark.asyncio
    async def test_async_variant(self):
        @requires_roles("admin")
        async def handler():
            return "ok"

        _authenticate(roles={"admin"})
        assert await handler() == "ok"


class TestRequiresPermissions:
    def test_allowed_on_a_matching_permission(self):
        @requires_permissions("orders:write")
        def handler():
            return "ok"

        _authenticate(permissions={"orders:write"})
        assert handler() == "ok"

    def test_refused_without_the_permission(self):
        @requires_permissions("orders:write")
        def handler():
            return "ok"

        _authenticate(permissions={"orders:read"})
        with pytest.raises(AuthorizationError):
            handler()

    @pytest.mark.asyncio
    async def test_async_variant(self):
        @requires_permissions("orders:write")
        async def handler():
            return "ok"

        _authenticate(permissions={"orders:write"})
        assert await handler() == "ok"
