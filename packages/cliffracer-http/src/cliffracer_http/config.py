from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class HttpConfig(BaseSettings):
    """Settings for the HTTP extension, loaded with CLIFFRACER_HTTP_ prefix."""

    model_config = SettingsConfigDict(env_prefix="CLIFFRACER_HTTP_")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
