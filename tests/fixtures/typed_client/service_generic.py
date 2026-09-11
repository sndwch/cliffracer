"""Fixture service with a parametrized generic model for testing CLI exit code 4."""

from __future__ import annotations

from pydantic import BaseModel

from cliffracer import CliffracerService, rpc


class Page[T](BaseModel):
    items: list[T]


class GenericService(CliffracerService):
    @rpc
    async def get_items(self, page: Page[int]) -> list[int]:
        return page.items
