import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit


class User(BaseModel):
    name: str


# Create a mock User in another module
class MockUser(BaseModel):
    __module__ = "other.models"
    __qualname__ = "User"
    name: str


class Solo(BaseModel):
    """A model whose name collides with no other imported name."""

    name: str


class SvcA(CliffracerService):
    @rpc
    async def get_user(self) -> User:
        return User(name="A")

    @rpc
    async def get_other_user(self) -> MockUser:
        return MockUser(name="B")

    @rpc
    async def get_solo(self) -> Solo:
        return Solo(name="C")


def test_namespace_collision():
    desc_a = describe(SvcA, service="svc-a", version="1")

    code_a = emit(desc_a)

    # Assert they use prefixed imports due to collision
    assert "User as TestsUnitTestNamespaceCollisionUser" in code_a
    assert "-> TestsUnitTestNamespaceCollisionUser:" in code_a

    assert "User as OtherModelsUser" in code_a
    assert "-> OtherModelsUser:" in code_a


def test_a_model_that_collides_with_nothing_keeps_its_own_name():
    """The other half of the rule: an alias is for a collision only. `Solo` is imported and used
    under its bare name while the two `User`s beside it are aliased, so a generator that aliased
    everything, always, passes the test above and fails this one."""
    code = emit(describe(SvcA, service="svc-a", version="1"))

    assert "-> Solo:" in code
    assert "Solo as" not in code
    assert "-> TestsUnitTestNamespaceCollisionSolo" not in code
