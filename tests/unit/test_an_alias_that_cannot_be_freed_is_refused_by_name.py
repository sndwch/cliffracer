"""An imported model's alias is prefixed until it is free, and the prefixing is bounded.

Prefixing makes the alias longer on every pass, so with a real module prefix a free alias is
always found within `len(taken) + 1` passes. The bound is there so that a prefix which stops
lengthening the alias ends in a `CannotEmit` naming the model and its module, not a generator that
never returns. The row reaches the bound by replacing the prefix with an empty one, the one way to
make it lengthen nothing.
"""

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer.generate_client import emitter
from cliffracer.generate_client.emitter import CannotEmit, emit
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit


class User(BaseModel):
    name: str


class OtherUser(BaseModel):
    __module__ = "other.models"
    __qualname__ = "User"
    name: str


class Accounts(CliffracerService):
    @rpc
    async def get_user(self) -> User:
        return User(name="a")

    @rpc
    async def get_other_user(self) -> OtherUser:
        return OtherUser(name="b")


def test_a_prefix_that_lengthens_nothing_is_refused_naming_the_model_and_its_module(monkeypatch):
    desc = describe(Accounts, service="accounts", version="1")
    assert "User as OtherModelsUser" in emit(desc)

    monkeypatch.setattr(emitter, "_module_prefix", lambda module: "")
    with pytest.raises(CannotEmit) as refused:
        emit(desc)
    # `other.models` sorts first and takes the bare `User`; this module's `User` is the one left
    # with nowhere to go.
    assert str(refused.value).startswith(f"no free alias for 'User' imported from {__name__!r}: ")
