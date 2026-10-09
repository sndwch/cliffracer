"""refresh_token returned None for every valid token. It should not."""

import time

import jwt
import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

pytestmark = pytest.mark.unit

SECRET = "x" * 40


def _service(**overrides):
    svc = SimpleAuthService(AuthConfig(secret_key=SECRET, **overrides))
    svc.create_user("alice", "alice@example.com", "s3cret-password", roles={"user"})
    return svc


def _decode(svc, token):
    return jwt.decode(token, SECRET, algorithms=[svc.config.algorithm])


class TestRefreshSucceeds:
    def test_a_valid_token_refreshes(self):
        """Verify that a valid token can be refreshed."""
        svc = _service()
        original = svc.authenticate("alice", "s3cret-password")
        assert original is not None

        time.sleep(1.1)  # so exp actually moves
        refreshed = svc.refresh_token(original)

        assert refreshed is not None
        assert _decode(svc, refreshed)["exp"] > _decode(svc, original)["exp"]

    def test_the_refreshed_token_still_validates(self):
        svc = _service()
        refreshed = svc.refresh_token(svc.authenticate("alice", "s3cret-password"))
        context = svc.validate_token(refreshed)
        assert context is not None
        assert context.user.username == "alice"

    def test_no_authentication_failure_is_logged(self):
        """Half the reported defect was a WARNING pointing operators at a
        credential problem that did not exist."""
        from loguru import logger as _logger

        svc = _service()
        token = svc.authenticate("alice", "s3cret-password")

        messages = []
        sink_id = _logger.add(messages.append, level="WARNING")
        try:
            refreshed = svc.refresh_token(token)
        finally:
            _logger.remove(sink_id)

        # The decision, and then every warning: a message literal can be reworded while the
        # misleading warning is still what the operator reads.
        assert refreshed is not None
        assert messages == [], [str(m) for m in messages]


class TestRefreshRefuses:
    def test_an_invalid_token_returns_none(self):
        svc = _service()
        assert svc.refresh_token("not-a-jwt") is None

    def test_a_deactivated_user_cannot_refresh(self):
        """Otherwise a deactivated account extends its access indefinitely."""
        svc = _service()
        token = svc.authenticate("alice", "s3cret-password")
        svc._users["alice"]["user"].is_active = False
        assert svc.refresh_token(token) is None

    def test_a_deleted_user_cannot_refresh(self):
        svc = _service()
        token = svc.authenticate("alice", "s3cret-password")
        del svc._users["alice"]
        assert svc.refresh_token(token) is None


class TestRefreshReadsCurrentState:
    def test_role_changes_since_login_are_reflected(self):
        """Re-signing a stale snapshot would silently extend revoked access."""
        svc = _service()
        token = svc.authenticate("alice", "s3cret-password")
        svc._users["alice"]["user"].roles = {"admin"}

        refreshed = svc.refresh_token(token)
        assert set(_decode(svc, refreshed)["roles"]) == {"admin"}


class TestLifetimeCap:
    def test_bounded_by_default_at_thirty_days(self):
        svc = _service()
        assert svc.config.refresh_max_lifetime_hours == 720
        token = svc.authenticate("alice", "s3cret-password")
        assert svc.refresh_token(token) is not None

    def test_oiat_is_carried_across_refreshes(self):
        svc = _service()
        first = svc.authenticate("alice", "s3cret-password")
        origin = _decode(svc, first)["oiat"]

        second = svc.refresh_token(first)
        third = svc.refresh_token(second)
        assert _decode(svc, third)["oiat"] == origin

    def test_a_refresh_past_the_window_is_refused(self):
        svc = _service(refresh_max_lifetime_hours=1)
        token = svc.authenticate("alice", "s3cret-password")

        # Re-mint with an original issue time two hours ago.
        stale = svc._mint_token(svc._users["alice"]["user"], original_iat=time.time() - 7200)
        assert svc.refresh_token(stale) is None
        assert svc.refresh_token(token) is not None  # inside the window


def _without_oiat(svc, *, iat: float) -> str:
    """A token with a jti but no oiat, signed with the service's secret.

    The service's minter never produces one: every minted token carries both.
    Only something signing tokens itself with the shared secret could.
    """
    user = svc._users["alice"]["user"]
    return jwt.encode(
        {
            "jti": "a-jti-without-oiat",
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "roles": list(user.roles),
            "permissions": list(user.permissions),
            "exp": time.time() + 3600,
            "iat": iat,
        },
        SECRET,
        algorithm=svc.config.algorithm,
    )


class TestATokenWithoutOiat:
    """The refresh lifetime cap is measured from oiat, so a token without one is not refreshed."""

    def test_it_is_not_refreshed(self):
        """Uncapped, so no lifetime could refuse it for another reason."""
        svc = _service()
        assert svc.refresh_token(_without_oiat(svc, iat=time.time())) is None

    def test_one_issued_past_the_cap_is_not_refreshed(self):
        """The shape a missing oiat must never turn into: a cap that no longer applies.

        With the lifetime read as `payload.get("oiat")` alone, this token has no
        original issue time, the cap check is skipped, and it refreshes two
        hours after issue under a one-hour cap.
        """
        svc = _service(refresh_max_lifetime_hours=1)
        assert svc.refresh_token(_without_oiat(svc, iat=time.time() - 7200)) is None

    def test_CONTROL_a_minted_token_with_oiat_still_refreshes(self):
        svc = _service(refresh_max_lifetime_hours=1)
        token = svc.authenticate("alice", "s3cret-password")
        assert "oiat" in _decode(svc, token)
        assert svc.refresh_token(token) is not None


def test_the_unused_refresh_token_map_is_gone():
    """Dead state named as if it backed the refresh flow, next to a refresh flow
    that now works, is worse than the status quo where nothing worked."""
    svc = _service()
    assert not hasattr(svc, "_refresh_tokens")
