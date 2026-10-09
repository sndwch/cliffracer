"""A username names one account, whatever case it is typed in.

`create_user` stores a user under the lowercased name, and every other method
looked the user up by the name exactly as typed. So "Alice" could register and
then not log in as "Alice", and `add_role("Alice", ...)` and
`add_permission("Alice", ...)` found no such user and did nothing, raising
nothing. Each method now looks the user up by the same normalised key, and
granting a role or permission to a user that does not exist raises.
"""

import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

from cliffracer.core.validation import validate_username

pytestmark = pytest.mark.unit

PASSWORD = "password123"


@pytest.fixture
def service() -> SimpleAuthService:
    svc = SimpleAuthService(AuthConfig(secret_key="x" * 32))
    svc.create_user("Alice", "alice@example.com", PASSWORD)
    return svc


def test_validate_username_returns_the_lowercased_name():
    assert validate_username("Alice.Smith-1") == "alice.smith-1"


@pytest.mark.parametrize("typed", ["Alice", "alice", "ALICE"])
def test_a_user_authenticates_under_any_case(service, typed):
    token = service.authenticate(typed, PASSWORD)

    assert token is not None
    assert service.validate_token(token).user.username == "alice"


def test_the_same_name_in_another_case_is_the_same_account(service):
    """The control: lowercasing is what stops two accounts sharing one name."""
    with pytest.raises(ValueError, match="already exists"):
        service.create_user("ALICE", "other@example.com", PASSWORD)


@pytest.mark.parametrize("typed", ["Alice", "ALICE"])
def test_a_role_granted_under_any_case_reaches_the_user(service, typed):
    service.add_role(typed, "admin")

    token = service.authenticate("alice", PASSWORD)
    assert "admin" in service.validate_token(token).user.roles


@pytest.mark.parametrize("typed", ["Alice", "ALICE"])
def test_a_permission_granted_under_any_case_reaches_the_user(service, typed):
    service.add_permission(typed, "orders:write")

    token = service.authenticate("alice", PASSWORD)
    assert "orders:write" in service.validate_token(token).user.permissions


def test_a_role_for_a_user_that_does_not_exist_is_refused(service):
    with pytest.raises(ValueError, match="nobody"):
        service.add_role("nobody", "admin")


def test_a_permission_for_a_user_that_does_not_exist_is_refused(service):
    with pytest.raises(ValueError, match="nobody"):
        service.add_permission("nobody", "orders:write")
