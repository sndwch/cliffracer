"""`refresh_token` verifies the token's signature once: validation and the refresh read one payload.

It decoded the token in `validate_token` and again for the claims it needed, so every refresh paid
for two signature checks and could in principle judge two different decodes. One decode now
serves both, and the decision a token gets is the one `validate_token` makes.
"""

import jwt
import pytest
from cliffracer_auth import AuthConfig, SimpleAuthService

pytestmark = pytest.mark.unit

KEY = "0123456789abcdef0123456789abcdef"


def _service() -> tuple[SimpleAuthService, str]:
    service = SimpleAuthService(AuthConfig(secret_key=KEY))
    service.create_user("alice", "alice@example.com", "a-long-enough-password")
    token = service.authenticate("alice", "a-long-enough-password")
    assert token is not None
    return service, token


def _decodes(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = jwt.decode

    def counting(token, *args, **kwargs):
        calls.append(token)
        return real(token, *args, **kwargs)

    monkeypatch.setattr(jwt, "decode", counting)
    return calls


def test_a_refresh_decodes_the_token_once(monkeypatch):
    service, token = _service()
    calls = _decodes(monkeypatch)

    refreshed = service.refresh_token(token)

    assert refreshed is not None and refreshed != token
    assert len(calls) == 1, f"the token was verified {len(calls)} times"


def test_the_refreshed_token_is_valid_and_is_in_the_same_chain(monkeypatch):
    service, token = _service()
    refreshed = service.refresh_token(token)
    assert refreshed is not None

    first = jwt.decode(token, KEY, algorithms=["HS256"])
    second = jwt.decode(refreshed, KEY, algorithms=["HS256"])

    assert service.validate_token(refreshed) is not None
    assert second["cid"] == first["cid"]
    assert second["oiat"] == first["oiat"]


def test_CONTROL_a_token_validation_refuses_is_still_refused_by_a_refresh():
    service, token = _service()
    service.revoke_token(token)

    assert service.refresh_token(token) is None
    assert service.refresh_token("not-a-token") is None
