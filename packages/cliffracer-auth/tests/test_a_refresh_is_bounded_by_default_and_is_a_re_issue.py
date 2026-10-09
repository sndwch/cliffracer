"""Refresh is a re-issue bounded by `refresh_max_lifetime_hours`, which defaults to 30 days.

A refreshed-from token stays valid to its own expiry, and can be refreshed again, so the only bound
on how long a leaked token can be kept alive by refreshing it is the cap measured from the original
login. It was unset by default, which made a single login grant access for ever.
"""

import time

import jwt
import pytest
from cliffracer_auth.simple_auth import (
    DEFAULT_REFRESH_MAX_LIFETIME_HOURS,
    AuthConfig,
    SimpleAuthService,
)

pytestmark = pytest.mark.unit

SECRET = "the-signing-key-" + "k" * 24
HOUR = 3600.0


def _service(**config) -> SimpleAuthService:
    svc = SimpleAuthService(AuthConfig(secret_key=SECRET, **config))
    svc.create_user("alice", "alice@example.com", "a-long-enough-password")
    return svc


def _logged_in_hours_ago(svc: SimpleAuthService, hours: float) -> str:
    """A token of a chain whose login was `hours` ago, minted by the service itself."""
    return svc._mint_token(svc._users["alice"]["user"], original_iat=time.time() - hours * HOUR)


def test_the_default_is_thirty_days():
    assert DEFAULT_REFRESH_MAX_LIFETIME_HOURS == 720
    assert AuthConfig(secret_key=SECRET).refresh_max_lifetime_hours == 720


def test_a_chain_is_refreshed_up_to_the_cap_and_not_past_it():
    svc = _service()

    assert svc.refresh_token(_logged_in_hours_ago(svc, 719)) is not None
    assert svc.refresh_token(_logged_in_hours_ago(svc, 721)) is None


def test_a_deployment_that_asks_for_no_cap_gets_none():
    svc = _service(refresh_max_lifetime_hours=None)

    assert svc.refresh_token(_logged_in_hours_ago(svc, 24 * 365 * 10)) is not None


def test_a_deployment_that_sets_its_own_cap_gets_that_one():
    svc = _service(refresh_max_lifetime_hours=48)

    assert svc.refresh_token(_logged_in_hours_ago(svc, 47)) is not None
    assert svc.refresh_token(_logged_in_hours_ago(svc, 49)) is None


def test_a_refresh_does_not_move_the_login_the_cap_is_measured_from():
    svc = _service(refresh_max_lifetime_hours=48)
    login = time.time() - 47 * HOUR
    token = svc._mint_token(svc._users["alice"]["user"], original_iat=login)

    refreshed = svc.refresh_token(token)

    assert refreshed is not None
    assert jwt.decode(refreshed, SECRET, algorithms=["HS256"])["oiat"] == pytest.approx(login)


def test_refresh_is_a_re_issue_the_token_it_was_given_stays_valid():
    """Documented: refresh does not consume its input. Both tokens live to their own expiry."""
    svc = _service()
    token = svc.authenticate("alice", "a-long-enough-password")
    assert token is not None

    refreshed = svc.refresh_token(token)

    assert refreshed is not None and refreshed != token
    assert svc.validate_token(token) is not None
    assert svc.validate_token(refreshed) is not None
    assert svc.refresh_token(token) is not None, "the same token can be refreshed again"


def test_revoking_either_token_ends_the_whole_chain():
    svc = _service()
    token = svc.authenticate("alice", "a-long-enough-password")
    assert token is not None
    refreshed = svc.refresh_token(token)
    assert refreshed is not None

    svc.revoke_token(refreshed)

    assert svc.validate_token(token) is None
    assert svc.validate_token(refreshed) is None
