"""Auth policy changes reach services that are already bound."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from cliffracer_auth import AuthConfig, AuthContext, AuthExtension, AuthUser, SimpleAuthService

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit

SECRET = "warehouse-auth-key-longer-than-thirty-two-characters"


class PolicyIssuer:
    def __init__(self) -> None:
        self.revoked: set[str] = set()

    def validate_token(self, token: str) -> AuthContext | None:
        if token in self.revoked:
            return None
        return AuthContext(
            user=AuthUser(
                user_id="ana",
                username="ana",
                email="ana@example.invalid",
            ),
            token=token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    def __call__(self) -> "PolicyIssuer":
        raise AssertionError("a constructed issuer must not be invoked as a factory")


def _message(token: str) -> AsyncMock:
    message = AsyncMock()
    message.subject = "warehouse.rpc.release_order"
    message.data = b"{}"
    message.headers = {"authorization": f"Bearer {token}"}
    return message


def _reply(message: AsyncMock) -> dict[str, object]:
    payload = message.respond.await_args.args[0]
    return json.loads(payload.decode())


async def test_a_revocation_on_the_configured_issuer_reaches_a_bound_service() -> None:
    issuer = SimpleAuthService(AuthConfig(secret_key=SECRET))
    issuer.create_user(
        "ana",
        "ana@example.invalid",
        "warehouse-password",
        roles={"dispatcher"},
    )

    class Warehouse(CliffracerService):
        auth = AuthExtension(issuer)

        @rpc
        async def release_order(self) -> str:
            return "released"

    warehouse = Warehouse(ServiceConfig(name="warehouse"))
    await warehouse.container._setup_extensions()
    warehouse._discover_handlers()

    token = issuer.authenticate("ana", "warehouse-password")
    assert token is not None

    accepted = _message(token)
    await warehouse.container._handle_rpc_request(accepted)
    assert _reply(accepted).get("result") == "released"

    assert issuer.revoke_token(token) is True

    revoked = _message(token)
    await warehouse.container._handle_rpc_request(revoked)
    reply = _reply(revoked)
    assert reply.get("result") != "released"
    assert "unauthenticated" in json.dumps(reply)


async def test_a_custom_issuer_policy_change_reaches_a_bound_service() -> None:
    issuer = PolicyIssuer()

    class Warehouse(CliffracerService):
        auth = AuthExtension(issuer)  # type: ignore[arg-type]

        @rpc
        async def release_order(self) -> str:
            return "released"

    warehouse = Warehouse(ServiceConfig(name="warehouse"))
    await warehouse.container._setup_extensions()
    warehouse._discover_handlers()

    accepted = _message("stock-token")
    await warehouse.container._handle_rpc_request(accepted)
    assert _reply(accepted).get("result") == "released"
    assert warehouse.auth.auth is issuer

    issuer.revoked.add("stock-token")

    revoked = _message("stock-token")
    await warehouse.container._handle_rpc_request(revoked)
    assert _reply(revoked).get("result") != "released"


def test_each_bound_extension_uses_the_configured_issuer_by_identity() -> None:
    issuer = SimpleAuthService(AuthConfig(secret_key=SECRET))

    class Warehouse(CliffracerService):
        auth = AuthExtension(auth=issuer)

    north = Warehouse(ServiceConfig(name="warehouse_north"))
    south = Warehouse(ServiceConfig(name="warehouse_south"))

    assert north.auth is not south.auth
    assert north.auth.auth is issuer
    assert south.auth.auth is issuer


@pytest.mark.parametrize("use_keyword", [False, True], ids=["positional", "keyword"])
def test_an_issuer_factory_still_builds_private_state_for_each_service(
    use_keyword: bool,
) -> None:
    issuers: list[SimpleAuthService] = []

    def new_issuer() -> SimpleAuthService:
        issuer = SimpleAuthService(AuthConfig(secret_key=SECRET))
        issuers.append(issuer)
        return issuer

    declaration = AuthExtension(auth=new_issuer) if use_keyword else AuthExtension(new_issuer)

    class Warehouse(CliffracerService):
        auth = declaration

    assert issuers == []

    north = Warehouse(ServiceConfig(name="warehouse_north"))
    south = Warehouse(ServiceConfig(name="warehouse_south"))

    assert issuers == [north.auth.auth, south.auth.auth]
    assert north.auth.auth is not south.auth.auth
