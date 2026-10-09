"""A service whose method names are the names its annotations read.

`from __future__ import annotations` keeps the service module importable: the methods are named
`list`, `dict` and `str`, which a signature written after them would otherwise read as the method.
A generated client has no such import, since `inspect.signature` is meant to return classes.
"""

from __future__ import annotations

from pydantic import BaseModel

from cliffracer import CliffracerService, rpc


class VERSION(BaseModel):
    """A model with the name of a class attribute of every generated client."""

    major: int


class Catalog(CliffracerService):
    @rpc
    async def list(self) -> list[str]:
        return []

    @rpc
    async def dict(self, filters: dict[str, int]) -> dict[str, int]:
        return filters

    @rpc
    async def str(self, text: str) -> str:
        return text

    @rpc
    async def search(self, tags: list[str], limit: int = 5) -> int:
        return limit

    @rpc
    async def lookup(self, key: VERSION) -> list[VERSION]:
        return [key]


class NAMESPACE(BaseModel):
    """A model with the name of the class attribute a client with a namespace binds."""

    team: str


class Scoped(CliffracerService):
    @rpc
    async def get(self, key: NAMESPACE) -> NAMESPACE:
        return key


class OnlyInsideOptional(CliffracerService):
    """`list` is read by `search` only inside an `| None`, and emitted before it, being sorted."""

    @rpc
    async def list(self) -> int:
        return 0

    @rpc
    async def search(self, tags: list[str] | None = None) -> int:
        return 0
