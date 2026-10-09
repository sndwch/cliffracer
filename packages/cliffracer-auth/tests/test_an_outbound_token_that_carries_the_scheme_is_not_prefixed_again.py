"""`outbound_token_factory` and a timer's `token_factory` take the same token, in the same forms.

A timer sends `Bearer <token>` once: a factory may return `abc` or `Bearer abc`. The outbound
factory prefixed whatever it was given, so a factory written to the timer's rule produced
`Bearer Bearer abc`, which the receiving service rejected.
"""

import pytest
from cliffracer_auth import AuthConfig, AuthExtension, SimpleAuthService

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import WorkerContext

pytestmark = pytest.mark.unit

SECRET = "the-signing-key-" + "k" * 24


def _issuer() -> SimpleAuthService:
    issuer = SimpleAuthService(AuthConfig(secret_key=SECRET))
    issuer.create_user("orders-svc", "orders@internal.example", "a-long-enough-password")
    return issuer


async def _sent_with(factory) -> dict[str, str]:
    """The headers an outbound call left with, after the send hooks ran."""

    class Svc(CliffracerService):
        auth = AuthExtension(_issuer(), outbound_token_factory=factory)

    svc = Svc(ServiceConfig(name="orders", health_port=0))
    await svc.container._setup_extensions()
    ctx = WorkerContext(
        kind="call_rpc",
        subject="inventory.rpc.check",
        headers={},
        correlation_id="corr",
        payload={},
    )
    seen: dict[str, str] = {}

    async def send() -> None:
        seen.update(ctx.headers)

    await svc.container._run_send_hooks(ctx, send)
    return seen


@pytest.mark.parametrize(
    ("minted", "sent"),
    [
        ("abc", "Bearer abc"),
        ("Bearer abc", "Bearer abc"),
        ("bearer abc", "bearer abc"),
        ("BEARER abc", "BEARER abc"),
    ],
    ids=["bare", "prefixed", "lower-case", "upper-case"],
)
async def test_the_token_is_sent_with_the_scheme_once(minted, sent):
    headers = await _sent_with(lambda: minted)

    assert headers["authorization"] == sent


async def test_an_awaitable_that_returns_a_prefixed_token_is_sent_once_too():
    async def mint() -> str:
        return "Bearer abc"

    assert (await _sent_with(mint))["authorization"] == "Bearer abc"


@pytest.mark.parametrize("token", ["abc", "Bearer abc", "bearer abc"])
def test_the_helper_the_timer_and_the_extension_share_agrees_with_both(token):
    from cliffracer.core.credentials import bearer

    assert bearer(token) == (token if token.lower().startswith("bearer ") else f"Bearer {token}")
