"""`AuthExtension(outbound_token_factory=...)` puts a service-identity token on every outbound message.

An authenticated service calling another authenticated service had to hand-write a `before_call`
extension to attach `authorization`. The factory mirrors `token_factory` on timers: the token is the
service's own, minted when the message is sent, and nothing is forwarded from the message being
handled.
"""

import pytest
from cliffracer_auth import AuthConfig, AuthExtension, SimpleAuthService
from cliffracer_auth.simple_auth import AuthContext, AuthUser, auth_context_var

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import RejectMessage, WorkerContext

pytestmark = pytest.mark.unit

SECRET = "the-signing-key-" + "k" * 24
SEND_KINDS = ["call_rpc", "call_async", "call_rpc_no_wait", "publish_event", "broadcast"]


def _issuer() -> SimpleAuthService:
    issuer = SimpleAuthService(AuthConfig(secret_key=SECRET))
    issuer.create_user("orders-svc", "orders@internal.example", "a-long-enough-password")
    return issuer


async def _service(**extension_args):
    issuer = extension_args.pop("issuer", None) or _issuer()

    class Svc(CliffracerService):
        auth = AuthExtension(issuer, **extension_args)

    svc = Svc(ServiceConfig(name="orders", health_port=0))
    await svc.container._setup_extensions()
    return svc


async def _sent(
    svc, kind: str = "call_rpc", headers: dict[str, str] | None = None
) -> dict[str, str]:
    """The headers the message left with, after the send hooks ran."""
    ctx = WorkerContext(
        kind=kind,
        subject="inventory.rpc.check",
        headers=dict(headers or {}),
        correlation_id="corr",
        payload={},
    )
    seen: dict[str, str] = {}

    async def send():
        seen.update(ctx.headers)

    await svc.container._run_send_hooks(ctx, send)
    return seen


@pytest.mark.parametrize("kind", SEND_KINDS)
async def test_every_kind_of_send_carries_the_token_the_factory_minted(kind):
    svc = await _service(outbound_token_factory=lambda: "service-token")

    headers = await _sent(svc, kind)

    assert headers["authorization"] == "Bearer service-token"


@pytest.mark.parametrize("kind", SEND_KINDS)
async def test_without_a_factory_nothing_is_attached_and_nothing_is_logged(kind):
    from loguru import logger

    svc = await _service()
    said: list[str] = []
    handler = logger.add(lambda m: said.append(m.record["message"]), level="WARNING")
    try:
        headers = await _sent(svc, kind)
    finally:
        logger.remove(handler)

    assert not [key for key in headers if key.lower() == "authorization"]
    assert said == []


async def test_the_factory_is_called_for_each_send_so_a_token_is_fresh():
    minted = []

    def factory():
        minted.append(f"token-{len(minted)}")
        return minted[-1]

    svc = await _service(outbound_token_factory=factory)

    first = await _sent(svc)
    second = await _sent(svc)

    assert (first["authorization"], second["authorization"]) == ("Bearer token-0", "Bearer token-1")


async def test_an_async_factory_is_awaited():
    async def factory():
        return "from-an-async-factory"

    svc = await _service(outbound_token_factory=factory)

    assert (await _sent(svc))["authorization"] == "Bearer from-an-async-factory"


@pytest.mark.parametrize("nothing", [None, ""], ids=["None", "empty string"])
async def test_a_factory_that_returns_nothing_sends_without_a_header(nothing):
    svc = await _service(outbound_token_factory=lambda: nothing)

    assert "authorization" not in await _sent(svc)


async def test_a_factory_that_raises_is_logged_and_the_call_goes_without_a_token():
    from loguru import logger

    def broken():
        raise RuntimeError("issuer is down")

    svc = await _service(outbound_token_factory=broken)
    said: list[str] = []
    handler = logger.add(lambda m: said.append(m.record["message"]), level="ERROR")
    try:
        headers = await _sent(svc)
    finally:
        logger.remove(handler)

    assert "authorization" not in headers
    assert any("outbound_token_factory raised RuntimeError: issuer is down" in m for m in said), (
        said
    )


async def test_a_header_the_message_already_carries_is_not_replaced():
    svc = await _service(outbound_token_factory=lambda: "service-token")

    headers = await _sent(svc, headers={"Authorization": "Bearer chosen-elsewhere"})

    assert headers["Authorization"] == "Bearer chosen-elsewhere"
    assert "authorization" not in headers


async def test_the_configured_header_name_is_the_one_written():
    svc = await _service(header="X-Service-Auth", outbound_token_factory=lambda: "service-token")

    headers = await _sent(svc)

    assert headers["x-service-auth"] == "Bearer service-token"
    assert "authorization" not in headers


async def test_the_callers_token_is_never_forwarded():
    """A handler running for a caller sends its own service's token, not the one it was given."""
    svc = await _service(outbound_token_factory=lambda: "service-token")
    caller = AuthContext(
        user=AuthUser(user_id="u9", username="a-customer", email="c@example.com"),
        token="the-callers-token",
        expires_at=None,
    )
    reset = auth_context_var.set(caller)
    try:
        headers = await _sent(svc)
    finally:
        auth_context_var.reset(reset)

    assert headers["authorization"] == "Bearer service-token"
    assert "the-callers-token" not in str(headers)


async def test_the_receiving_service_authenticates_the_sender_as_the_service_identity():
    """End to end through two extensions sharing one issuer: the header a sender wrote is the one
    the receiver's `worker_setup` accepts, and the identity it publishes is the service's."""
    issuer = _issuer()
    sender = await _service(
        issuer=issuer,
        outbound_token_factory=lambda: issuer.authenticate("orders-svc", "a-long-enough-password"),
    )
    receiver = await _service(issuer=issuer)

    headers = await _sent(sender)
    inbound = WorkerContext(
        kind="rpc", subject="inventory.rpc.check", headers=headers, correlation_id="c", payload={}
    )
    await receiver.auth.worker_setup(inbound)
    try:
        assert inbound.data["auth"].user.username == "orders-svc"
    finally:
        await receiver.auth.worker_teardown(inbound)


async def test_the_receiver_refuses_a_send_that_carried_no_factory_token():
    issuer = _issuer()
    sender = await _service(issuer=issuer)
    receiver = await _service(issuer=issuer)

    headers = await _sent(sender)
    inbound = WorkerContext(
        kind="rpc", subject="inventory.rpc.check", headers=headers, correlation_id="c", payload={}
    )

    with pytest.raises(RejectMessage, match="unauthenticated"):
        await receiver.auth.worker_setup(inbound)


async def test_a_factory_wrapped_in_shareddependency_is_the_same_as_a_bare_one():
    from cliffracer.core.extension import SharedDependency

    svc = await _service(outbound_token_factory=SharedDependency(lambda: "wrapped"))

    assert (await _sent(svc))["authorization"] == "Bearer wrapped"


# ---- the one argument that is passed through, and the others that are not ---------------------


async def test_another_zero_argument_callable_given_to_the_extension_is_still_called_per_service():
    """The exception is for `outbound_token_factory` alone. `default_timer_user` given as a
    zero-argument callable is called once per bound service, as the machinery does for any
    extension argument, and the service receives what it returned."""
    built: list[AuthUser] = []

    def build_user() -> AuthUser:
        user = AuthUser(user_id="bot", username="bot", email="bot@internal.example")
        built.append(user)
        return user

    svc = await _service(default_timer_user=build_user)

    assert svc.auth.default_timer_user is built[-1]
    assert isinstance(svc.auth.default_timer_user, AuthUser)


async def test_a_callable_argument_of_an_ordinary_extension_is_still_called_once_per_service():
    from cliffracer.core.extension import Extension

    class Plain(Extension):
        def __init__(self, make=None) -> None:
            self.made = make

    calls: list[int] = []

    class Svc(CliffracerService):
        plain = Plain(make=lambda: calls.append(1) or "built per service")

    first = Svc(ServiceConfig(name="first", health_port=0))
    second = Svc(ServiceConfig(name="second", health_port=0))

    assert first.plain.made == second.plain.made == "built per service"
    assert len(calls) == 2


async def test_the_token_factory_is_not_called_when_the_service_is_bound():
    """Binding does not mint a token: the factory runs when a message is sent."""
    calls: list[int] = []

    class Svc(CliffracerService):
        auth = AuthExtension(_issuer(), outbound_token_factory=lambda: calls.append(1) or "t")

    Svc(ServiceConfig(name="bound", health_port=0))
    Svc(ServiceConfig(name="bound-again", health_port=0))

    assert calls == []
