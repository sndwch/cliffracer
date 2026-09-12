"""Auth credentials must reach NATS, and must never reach the log."""

from unittest.mock import AsyncMock, patch

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.service import redact_nats_url

pytestmark = pytest.mark.unit


class TestRedactNatsUrl:
    def test_plain_url_is_unchanged(self):
        assert redact_nats_url("nats://localhost:4222") == "nats://localhost:4222"

    def test_password_is_removed(self):
        out = redact_nats_url("nats://user:s3cret@host:4222")
        assert "s3cret" not in out
        assert "host:4222" in out

    def test_token_style_userinfo_is_removed(self):
        out = redact_nats_url("nats://tokenvalue@host:4222")
        assert "tokenvalue" not in out
        assert "host:4222" in out

    def test_password_containing_a_comma_is_fully_redacted(self):
        out = redact_nats_url("nats://svc:p,w@broker:4222")
        assert "p,w" not in out and "svc" not in out
        assert "broker:4222" in out

    def test_garbage_input_does_not_raise(self):
        # Must never take down a service just to format a log line.
        assert isinstance(redact_nats_url("not a url"), str)

    def test_scheme_less_url_still_redacts(self):
        out = redact_nats_url("user:secret@host:4222")
        assert "secret" not in out
        assert "host:4222" in out

    def test_scheme_less_token_form_still_redacts(self):
        out = redact_nats_url("tokenvalue@host:4222")
        assert "tokenvalue" not in out
        assert "host:4222" in out

    # --- scheme allowlist ---
    #
    # Ensure URL schemes not in the NATS allowlist are treated as credentials and redacted.

    def test_a_secret_shaped_like_a_scheme_is_not_echoed(self):
        out = redact_nats_url("svctoken://user:pass@host:4222")
        assert "svctoken" not in out
        assert "pass" not in out
        assert "host:4222" in out

    def test_a_bare_token_shaped_like_a_scheme_is_not_echoed(self):
        out = redact_nats_url("apikeytoken1234://@host:4222")
        assert "apikeytoken1234" not in out
        assert "host:4222" in out

    def test_every_allowed_scheme_survives(self):
        for scheme in ("nats", "tls", "ws", "wss"):
            out = redact_nats_url(f"{scheme}://user:secret@host:4222")
            assert out == f"{scheme}://***@host:4222"

    def test_allowed_schemes_are_case_insensitive(self):
        assert redact_nats_url("NATS://user:secret@host:4222") == "NATS://***@host:4222"

    def test_an_unknown_scheme_is_treated_as_a_secret(self):
        # http is a real scheme but not one NATS speaks, so it is not on the
        # allowlist and gets redacted rather than echoed.
        out = redact_nats_url("http://user:secret@host:4222")
        assert "secret" not in out
        assert out.startswith("***@")

    def test_a_url_with_no_credentials_is_returned_unchanged(self):
        # No "@" means nothing to hide; it never reaches the placeholder.
        assert redact_nats_url("nats://localhost:4222") == "nats://localhost:4222"

    def test_none_is_returned_as_its_string_form_not_the_placeholder(self):
        # str(None) contains no '@', returning early without placeholder replacement.
        assert redact_nats_url(None) == "None"

    def test_password_containing_a_scheme_separator_is_redacted(self):
        out = redact_nats_url("user:pa://ss@host:4222")
        assert "user:pa" not in out and "ss" not in out.split("@")[0]
        assert "host:4222" in out

    def test_at_sign_before_the_scheme_is_redacted(self):
        out = redact_nats_url("us:er@nats://user:pass@host:4222")
        assert "us:er" not in out and "pass" not in out
        assert "host:4222" in out


class TestConnectForwardsCredentials:
    @pytest.mark.asyncio
    async def test_no_credentials_passes_no_auth_kwargs(self):
        svc = CliffracerService(ServiceConfig(name="s"))
        with patch("nats.connect", new=AsyncMock()) as m:
            await svc.connect()
        kwargs = m.call_args.kwargs
        for key in ("user", "password", "token", "user_credentials"):
            assert key not in kwargs, f"{key} must be absent when unset"

    @pytest.mark.asyncio
    async def test_user_password_are_forwarded(self):
        cfg = ServiceConfig(name="s", nats_user="u", nats_password="p")
        svc = CliffracerService(cfg)
        with patch("nats.connect", new=AsyncMock()) as m:
            await svc.connect()
        assert m.call_args.kwargs["user"] == "u"
        assert m.call_args.kwargs["password"] == "p"

    @pytest.mark.asyncio
    async def test_token_and_credentials_file_are_forwarded(self):
        cfg = ServiceConfig(name="s", nats_token="t", nats_credentials_file="/c.creds")
        svc = CliffracerService(cfg)
        with patch("nats.connect", new=AsyncMock()) as m:
            await svc.connect()
        assert m.call_args.kwargs["token"] == "t"
        assert m.call_args.kwargs["user_credentials"] == "/c.creds"

    @pytest.mark.asyncio
    async def test_password_never_reaches_the_log(self):
        cfg = ServiceConfig(name="s", nats_url="nats://u:supersecret@h:4222")
        svc = CliffracerService(cfg)
        seen: list[str] = []
        with patch("nats.connect", new=AsyncMock()):
            with patch.object(
                svc.logger, "info", side_effect=lambda m, *a, **k: seen.append(str(m))
            ):
                await svc.connect()
        assert seen, "connect() should log something"
        assert not any("supersecret" in line for line in seen)
