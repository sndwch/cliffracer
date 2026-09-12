import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit


# Simulate two models from different modules
class User1(BaseModel):
    __module__ = "auth"
    __qualname__ = "User"
    name: str


class User2(BaseModel):
    __module__ = "billing"
    __qualname__ = "User"
    name: str


class SvcMixed(CliffracerService):
    @rpc
    async def get_auth_user(self) -> User1:
        return User1(name="A")

    @rpc
    async def get_billing_user(self) -> User2:
        return User2(name="B")


def test_namespace_collision_same_service():
    desc = describe(SvcMixed, service="mixed", version="1")
    code = emit(desc)

    assert "from auth import User as AuthUser" in code
    assert "from billing import User as BillingUser" in code
