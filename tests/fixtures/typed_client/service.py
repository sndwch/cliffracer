"""The service the end-to-end proof generates a client for.

Non-trivial on purpose: nested models, a list return, an optional return, a
default argument, an error path, and authentication in front of all of it. A
generator that only ever meets `str -> str` proves nothing.
"""

from cliffracer_auth import AuthConfig, AuthExtension, SimpleAuthService

from cliffracer import CliffracerService, ServiceConfig, rpc

from .models import Line, Order, Receipt

# At least 32 characters: SimpleAuthService refuses anything shorter, and a
# fixture that cannot construct its own auth service is a fixture that hides
# the thing it exists to exercise.
SECRET = "typed-client-test-secret-0123456789abcdef"
USERNAME = "e2e"
PASSWORD = "e2e-password-1234"

_auth = SimpleAuthService(AuthConfig(secret_key=SECRET))


class Warehouse(CliffracerService):
    auth = AuthExtension(_auth)

    @rpc
    async def create(self, order: Order, note: str = "") -> Receipt:
        """Create an order and return its receipt."""
        order_id = order.order_id or f"o-{len(order.lines)}"
        return Receipt(
            order_id=order_id,
            total_qty=sum(line.qty for line in order.lines),
            tags={"note": note} if note else {},
        )

    @rpc
    async def lines(self, skus: list[str]) -> list[Line]:
        """One Line per sku, at the default quantity."""
        return [Line(sku=sku) for sku in skus]

    @rpc
    async def find(self, order_id: str) -> Receipt | None:
        """None for a missing order, which is what the optional return is for."""
        return None if order_id == "missing" else Receipt(order_id=order_id, total_qty=0)

    @rpc
    async def fail(self, reason: str) -> str:
        """Raise, so the client's error path has something real to carry."""
        raise RuntimeError(reason)


def make(name: str = "warehouse_e2e") -> Warehouse:
    return Warehouse(ServiceConfig(name=name, version="3.1.4"))


def token() -> str:
    """A bearer token for the fixture user, created on first use.

    `create_user` then `authenticate` -- read from `SimpleAuthService`, not
    guessed: `authenticate(username, password)` is what returns the JWT.
    """
    if USERNAME not in _auth._users:
        _auth.create_user(USERNAME, "e2e@example.com", PASSWORD)
    issued = _auth.authenticate(USERNAME, PASSWORD)
    if issued is None:
        raise RuntimeError("the fixture user could not authenticate")
    return issued
