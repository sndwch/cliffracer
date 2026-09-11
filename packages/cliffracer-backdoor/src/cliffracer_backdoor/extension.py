"""The debug console as an extension."""

from __future__ import annotations

from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from cliffracer.core.extension import Extension, ExtensionSetupContext
from cliffracer_backdoor.backdoor import BackdoorServer


class BackdoorConfig(BaseSettings):
    """Settings for the debug console, loaded from CLIFFRACER_BACKDOOR_* or keyword arguments.

    Disabled by default to prevent arbitrary Python code execution.
    """

    model_config = SettingsConfigDict(env_prefix="CLIFFRACER_BACKDOOR_")

    enabled: bool = False
    port: int = 0  # 0 asks the OS for a free port
    password: str | None = None


class BackdoorExtension(Extension):
    """Run a debug console alongside the service.

        class Orders(CliffracerService):
            backdoor = BackdoorExtension(enabled=True)

    Then `cliffracer-backdoor 127.0.0.1:<port>`, where the port is in
    `/health` under this extension's declared name.
    """

    def __init__(self, **overrides: Any) -> None:
        # Configuration only. Per-instance state is created in setup(), because
        # bind() is a shallow copy and anything built here is shared by every
        # service that declares this extension.
        self.config = BackdoorConfig(**overrides)

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        self.service = ctx.service
        self._server: BackdoorServer | None = None
        self._port: int | None = None

    async def start(self) -> None:
        if not self.config.enabled:
            return
        self._server = BackdoorServer(
            service_instance=self.service,
            port=self.config.port,
            password=self.config.password,
            enabled=True,
        )
        self._port = await self._server.start()

    async def stop(self) -> None:
        if self._server is None:
            return
        await self._server.stop()
        self._server = None
        self._port = None

    def health_details(self) -> dict[str, Any] | None:
        """The bound port, or why there is not one.

        Reports `port` only after start() has bound one -- `config.port` is 0
        by default and would say nothing about whether a console is listening.
        """
        if not self.config.enabled:
            return {"enabled": False}
        return {"enabled": True, "port": self._port}
