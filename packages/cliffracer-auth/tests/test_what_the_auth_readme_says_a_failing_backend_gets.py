"""The README says what a caller gets when the token backend raises: an `internal` error.

A backend that raises while checking a token is the service being broken, not the caller being
wrong, so it is answered as a crashed hook (`internal`) and not as the refusal an absent or
invalid token gets (`refused: unauthenticated`). The README said all three get the refusal.
"""

import json
from pathlib import Path

import pytest
from cliffracer_auth import AuthConfig, AuthExtension, SimpleAuthService

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit

README = Path(__file__).resolve().parents[1] / "README.md"


class BrokenIssuer(SimpleAuthService):
    def validate_token(self, token):
        raise RuntimeError("jwks fetch failed")


class Svc(CliffracerService):
    auth = AuthExtension(
        BrokenIssuer(AuthConfig(secret_key="a-signing-key-of-at-least-32-characters"))
    )

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="authd", health_port=0, subject_prefix=None))

    @rpc
    async def ping(self) -> dict[str, str]:
        return {"ok": "yes"}


async def _ask(headers: dict[str, str]) -> dict:
    svc = Svc()
    await svc.container._setup_extensions()
    svc._discover_handlers()
    message = MockMessage(
        "authd.rpc.ping",
        data=b"{}",
        headers={"Content-Type": "application/json", **headers},
        reply="_INBOX.r",
    )
    await svc.container._handle_rpc_request(message)
    return json.loads(message.responded_data)


async def test_a_message_with_no_token_gets_the_refusal():
    reply = await _ask({})

    assert (reply["error"], reply["code"]) == ("refused: unauthenticated", "refused")


async def test_a_backend_that_raises_gets_an_internal_error_not_the_refusal():
    reply = await _ask({"authorization": "Bearer abc"})

    assert (reply["error"], reply["code"]) == ("extension auth failed: internal error", "internal")


def test_the_readme_says_both_of_those_things():
    text = " ".join(README.read_text().split())

    assert (
        "Two things are refused: a message with no token and a message with an invalid one" in text
    )
    assert "`refused: unauthenticated`" in text
    assert "`internal` error (`extension auth failed: internal error`)" in text
    assert "All three get" not in text
