"""A user's roles and permissions are the user's own sets, not the caller's.

`create_user` stored the set it was given when that set was not empty, so a module-level
default shared across several calls became one set shared by all those users, and `add_role` on
one of them granted the role to every other and to the caller's constant.
"""

import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

pytestmark = pytest.mark.unit

SECRET = "x" * 40


@pytest.fixture
def svc():
    return SimpleAuthService(AuthConfig(secret_key=SECRET))


def _make(svc, name, **kwargs):
    return svc.create_user(name, f"{name}@example.com", "pw-long-enough-1", **kwargs)


def test_add_role_on_one_user_does_not_reach_another_created_from_the_same_set(svc):
    shared = {"user"}
    alice = _make(svc, "alice", roles=shared)
    bobby = _make(svc, "bobby", roles=shared)

    svc.add_role("alice", "admin")

    assert alice.roles == {"user", "admin"}
    assert bobby.roles == {"user"}


def test_add_permission_on_one_user_does_not_reach_another_created_from_the_same_set(svc):
    shared = {"orders:read"}
    alice = _make(svc, "alice", permissions=shared)
    bobby = _make(svc, "bobby", permissions=shared)

    svc.add_permission("alice", "orders:write")

    assert alice.permissions == {"orders:read", "orders:write"}
    assert bobby.permissions == {"orders:read"}


def test_add_role_does_not_change_the_callers_set(svc):
    shared = {"user"}
    _make(svc, "alice", roles=shared)

    svc.add_role("alice", "admin")

    assert shared == {"user"}


def test_changing_the_callers_set_afterwards_does_not_change_the_user(svc):
    roles, permissions = {"user"}, {"orders:read"}
    alice = _make(svc, "alice", roles=roles, permissions=permissions)

    roles.add("admin")
    permissions.add("orders:write")

    assert alice.roles == {"user"} and alice.permissions == {"orders:read"}


def test_CONTROL_an_empty_or_missing_set_gives_a_fresh_set_each_time(svc):
    alice = _make(svc, "alice")
    bobby = _make(svc, "bobby", roles=set(), permissions=set())

    assert alice.roles is not bobby.roles and alice.permissions is not bobby.permissions
    assert alice.roles == set() == bobby.roles


def test_CONTROL_the_roles_given_are_the_roles_the_user_has(svc):
    alice = _make(svc, "alice", roles={"user", "admin"}, permissions={"orders:read"})

    assert alice.roles == {"user", "admin"} and alice.permissions == {"orders:read"}
    assert svc._stored("alice")["user"] is alice
