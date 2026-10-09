"""Bind one local-supervisor owner to each parent service instance."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from cliffracer.core.extension import Extension, ExtensionSetupContext, SharedDependency

from .contracts import ActivationReference, ActivationUnavailable
from .supervisor import CleanupReport, LocalSupervisor, OwnerHandle


class ServiceOwner(Extension):
    """Share a supervisor explicitly; give each bound parent a separate lifetime."""

    def __init__(
        self,
        supervisor: SharedDependency[LocalSupervisor] | LocalSupervisor,
        *,
        scope: str,
        parent: OwnerHandle | None = None,
    ) -> None:
        self.supervisor = (
            supervisor.unwrap() if isinstance(supervisor, SharedDependency) else supervisor
        )
        self.scope = scope
        self.parent = parent
        self._owner: OwnerHandle | None = None
        self._closed = False
        self._close_task: asyncio.Task[CleanupReport] | None = None
        self.cleanup_report: CleanupReport | None = None

    @property
    def owner(self) -> OwnerHandle:
        if self._owner is None:
            raise ActivationUnavailable("parent ownership is not set up")
        return self._owner

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        self._owner = await self.supervisor.open_owner(self.scope, parent=self.parent)
        self._closed = False
        self._close_task = None
        self.cleanup_report = None

    def _admitting(self) -> None:
        lifecycle = self.service.container.lifecycle
        if (
            self._closed
            or lifecycle.stop_requested
            or not (lifecycle.is_running or lifecycle.is_starting)
        ):
            raise ActivationUnavailable("parent service admission is closed")

    async def ensure(
        self,
        template: str,
        key: str,
        settings: BaseModel | Mapping[str, Any],
        *,
        revision: str,
    ) -> ActivationReference:
        self._admitting()
        return await self.supervisor.ensure(self.owner, template, key, settings, revision=revision)

    async def reactivate(self, terminal: ActivationReference) -> ActivationReference:
        self._admitting()
        return await self.supervisor.reactivate(self.owner, terminal)

    async def _close_owner(self) -> CleanupReport:
        report = await self.supervisor.close_owner(self.owner)
        self.cleanup_report = report
        return report

    async def stop(self) -> None:
        self._closed = True
        if self._owner is not None:
            if self._close_task is None:
                self._close_task = asyncio.create_task(
                    self._close_owner(), name="parent_owner_cleanup"
                )
            await asyncio.shield(self._close_task)


__all__ = ["ServiceOwner"]
