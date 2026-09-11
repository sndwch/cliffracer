from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import describe


class User(BaseModel):
    name: str


# Create a mock User in another module
class MockUser(BaseModel):
    __module__ = "other.models"
    __qualname__ = "User"
    name: str


class SvcA(CliffracerService):
    @rpc
    async def get_user(self) -> User:
        return User(name="A")

    @rpc
    async def get_other_user(self) -> MockUser:
        return MockUser(name="B")


def test_namespace_collision():
    desc_a = describe(SvcA, service="svc-a", version="1")

    code_a = emit(desc_a)

    # Assert they use prefixed imports due to collision
    assert "User as TestsUnitTestNamespaceCollisionUser" in code_a
    assert "-> TestsUnitTestNamespaceCollisionUser:" in code_a

    assert "User as OtherModelsUser" in code_a
    assert "-> OtherModelsUser:" in code_a
