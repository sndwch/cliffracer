"""Tests for JWT token revocation and jti claim handling."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import jwt
import pytest
from cliffracer_auth.extension import AuthExtension
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit

SECRET = "x" * 40


def _service(**overrides):
    svc = SimpleAuthService(AuthConfig(secret_key=SECRET, **overrides))
    svc.create_user("alice", "alice@example.com", "s3cret-password", roles={"user", "admin"})
    return svc


class TestTokenRevocation:
    def test_minted_token_contains_unique_jti(self):
        svc = _service()
        t1 = svc.authenticate("alice", "s3cret-password")
        t2 = svc.authenticate("alice", "s3cret-password")
        assert t1 is not None and t2 is not None

        p1 = jwt.decode(t1, SECRET, algorithms=[svc.config.algorithm])
        p2 = jwt.decode(t2, SECRET, algorithms=[svc.config.algorithm])

        assert "jti" in p1
        assert "jti" in p2
        assert isinstance(p1["jti"], str)
        assert len(p1["jti"]) > 0
        assert p1["jti"] != p2["jti"]

    def test_revoked_token_is_rejected_by_validate_token(self):
        svc = _service()
        token = svc.authenticate("alice", "s3cret-password")
        assert token is not None
        assert svc.validate_token(token) is not None

        assert svc.revoke_token(token) is True
        assert svc.validate_token(token) is None

    def test_revoking_a_revoked_token_again_reports_it_revoked(self):
        svc = _service()
        token = svc.authenticate("alice", "s3cret-password")
        assert token is not None

        assert svc.revoke_token(token) is True
        assert svc.revoke_token(token) is True
        assert len(svc._revoked_jtis) == 1

    def test_revoking_one_token_does_not_invalidate_other_tokens_for_same_user(self):
        svc = _service()
        t1 = svc.authenticate("alice", "s3cret-password")
        t2 = svc.authenticate("alice", "s3cret-password")
        assert t1 is not None and t2 is not None

        svc.revoke_token(t1)
        assert svc.validate_token(t1) is None
        assert svc.validate_token(t2) is not None

    def test_refresh_token_refuses_revoked_token(self):
        svc = _service()
        token = svc.authenticate("alice", "s3cret-password")
        assert token is not None
        svc.revoke_token(token)
        assert svc.refresh_token(token) is None

    def test_revoking_an_expired_token_does_not_raise_and_stores_nothing(self):
        svc = _service()
        now = datetime.now(UTC)
        payload = {
            "jti": "custom-expired-jti-123",
            "user_id": "user_1",
            "username": "alice",
            "email": "alice@example.com",
            "exp": (now - timedelta(hours=1)).timestamp(),
            "iat": (now - timedelta(hours=2)).timestamp(),
        }
        expired_token = jwt.encode(payload, SECRET, algorithm=svc.config.algorithm)

        # An expired token is refused on its own, so there is nothing to keep.
        assert svc.revoke_token(expired_token) is True
        assert "custom-expired-jti-123" not in svc._revoked_jtis
        assert svc.validate_token(expired_token) is None

    def test_revoking_a_malformed_token_reports_failure_and_revokes_nothing(self):
        svc = _service()
        for token in ("not-a-jwt", "garbage.token.here", ""):
            assert svc.revoke_token(token) is False, token
        assert svc._revoked_jtis == {}

    def test_a_token_without_a_jti_cannot_be_revoked_and_is_not_accepted(self):
        """Revocation is keyed on the jti, so a token without one could never be
        revoked. It is refused, rather than accepted for as long as it lasts, and
        revoking it says it could not."""
        svc = _service()
        now = datetime.now(UTC)
        payload = {
            "user_id": "user_1",
            "username": "alice",
            "email": "alice@example.com",
            "exp": (now + timedelta(hours=1)).timestamp(),
            "iat": now.timestamp(),
        }
        no_jti = jwt.encode(payload, SECRET, algorithm=svc.config.algorithm)

        assert svc.revoke_token(no_jti) is False
        assert svc._revoked_jtis == {}
        assert svc.validate_token(no_jti) is None
        assert svc.refresh_token(no_jti) is None

    def test_CONTROL_the_same_token_with_a_jti_is_accepted(self):
        svc = _service()
        now = datetime.now(UTC)
        payload = {
            "jti": "a-jti",
            "user_id": "user_1",
            "username": "alice",
            "email": "alice@example.com",
            "exp": (now + timedelta(hours=1)).timestamp(),
            "iat": now.timestamp(),
        }
        with_jti = jwt.encode(payload, SECRET, algorithm=svc.config.algorithm)

        assert svc.validate_token(with_jti) is not None

    async def test_revoked_token_fails_authextension_dispatch(self):
        svc_auth = SimpleAuthService(AuthConfig(secret_key=SECRET))
        svc_auth.create_user("alice", "alice@example.com", "s3cret-password", roles={"user"})

        class ProtectedSvc(CliffracerService):
            auth = AuthExtension(svc_auth)

            @rpc
            async def secure_call(self) -> str:
                return "authorized"

        app = ProtectedSvc(ServiceConfig(name="test_svc"))
        await app.container._setup_extensions()
        app._discover_handlers()

        token = app.auth.auth.authenticate("alice", "s3cret-password")
        assert token is not None

        # Call with valid token succeeds
        msg = AsyncMock()
        msg.subject = "test_svc.rpc.secure_call"
        msg.data = json.dumps({}).encode()
        msg.headers = {"authorization": f"Bearer {token}"}
        await app.container._handle_rpc_request(msg)
        assert (
            json.loads(msg.respond.await_args_list[0].args[0].decode()).get("result")
            == "authorized"
        )

        # Now revoke the token on the service's auth instance
        app.auth.auth.revoke_token(token)

        # Next call with revoked token is rejected with unauthenticated
        msg_revoked = AsyncMock()
        msg_revoked.subject = "test_svc.rpc.secure_call"
        msg_revoked.data = json.dumps({}).encode()
        msg_revoked.headers = {"authorization": f"Bearer {token}"}
        await app.container._handle_rpc_request(msg_revoked)
        reply = json.loads(msg_revoked.respond.await_args_list[0].args[0].decode())
        assert "unauthenticated" in json.dumps(reply)
        assert reply.get("result") != "authorized"
