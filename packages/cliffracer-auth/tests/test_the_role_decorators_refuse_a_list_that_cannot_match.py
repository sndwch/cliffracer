"""`@requires_roles` and `@requires_permissions` refuse, where they are applied, a name list
that could never admit anyone.

`@requires_roles()` decorated cleanly and then refused every authenticated caller with
`Required roles: ()`: the check is "holds any one of these", and there were none. A name that
is not a string (`@requires_roles(["admin"])`) could not match either, and a list is not hashable,
so it failed on the first call instead. Both are configuration mistakes and are reported at
decoration time, next to the bare-use mistake that was already refused.
"""

from datetime import UTC, datetime, timedelta

import pytest
from cliffracer_auth.simple_auth import (
    AuthContext,
    AuthUser,
    clear_current_context,
    requires_permissions,
    requires_roles,
    set_current_context,
)

from cliffracer.core.exceptions import AuthorizationError, ConfigurationError

pytestmark = pytest.mark.unit

FACTORIES = [
    pytest.param(requires_roles, "requires_roles", id="roles"),
    pytest.param(requires_permissions, "requires_permissions", id="permissions"),
]


@pytest.fixture(autouse=True)
def _no_context():
    clear_current_context()
    yield
    clear_current_context()


@pytest.mark.parametrize(("factory", "name"), FACTORIES)
def test_a_decorator_with_no_names_is_refused_where_it_is_applied(factory, name):
    with pytest.raises(ConfigurationError, match=f"@{name} with no names"):
        factory()


@pytest.mark.parametrize(("factory", "name"), FACTORIES)
@pytest.mark.parametrize("bad", [["admin"], ("admin",), None, 5, b"admin"], ids=repr)
def test_a_name_that_is_not_a_string_is_refused_where_it_is_applied(factory, name, bad):
    with pytest.raises(ConfigurationError, match="separate string arguments"):
        factory(bad)


@pytest.mark.parametrize(("factory", "name"), FACTORIES)
def test_one_good_name_and_one_bad_name_is_refused_too(factory, name):
    with pytest.raises(ConfigurationError, match="separate string arguments"):
        factory("admin", ["support"])


def _sign_in(*, roles=(), permissions=()):
    user = AuthUser(
        user_id="user_1",
        username="alice",
        email="alice@example.com",
        roles=set(roles),
        permissions=set(permissions),
    )
    set_current_context(AuthContext(user=user, expires_at=datetime.now(UTC) + timedelta(hours=1)))


def test_CONTROL_names_given_as_separate_strings_still_decorate_and_admit_any_one():
    @requires_roles("admin", "support")
    def by_role():
        return "role"

    @requires_permissions("orders:read", "orders:write")
    def by_permission():
        return "permission"

    _sign_in(roles={"support"}, permissions={"orders:write"})
    assert by_role() == "role"
    assert by_permission() == "permission"


def test_CONTROL_a_caller_holding_none_of_the_names_is_still_refused():
    @requires_roles("admin")
    def by_role():
        return "role"

    _sign_in(roles={"user"})

    with pytest.raises(AuthorizationError, match="Required roles"):
        by_role()
