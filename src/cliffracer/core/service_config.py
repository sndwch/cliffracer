"""
Service configuration for Cliffracer services
"""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field


class ServiceConfig(BaseModel):
    """Configuration for NATS-based services"""

    name: str
    nats_url: str = Field(default="nats://localhost:4222")
    queue_group: str | None = None

    # Connection settings
    max_reconnect_attempts: int = Field(default=60)
    reconnect_time_wait: int = Field(default=2)

    # Health check settings
    health_check_interval: int = Field(default=30)
    health_check_timeout: int = Field(default=5)

    # Lifecycle hooks
    on_connect: Callable[[], Any] | None = None
    on_disconnect: Callable[[], Any] | None = None
    on_error: Callable[[Exception], Any] | None = None

    # Service metadata
    version: str = Field(default="0.1.0")

    # Backdoor debugging configuration
    backdoor_enabled: bool = Field(default=False)  # Disabled by default for security
    backdoor_port: int = Field(default=0)  # 0 for auto-assign
    disable_backdoor: bool = Field(default=False)  # Global disable flag
    description: str | None = None

    class Config:
        arbitrary_types_allowed = True
