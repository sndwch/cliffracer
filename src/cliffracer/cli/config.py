import types
from typing import Any

from cliffracer.core import ServiceConfig

# PyYAML is an optional CLI dependency imported on demand so that
# `cliffracer run` without --config does not require it.
_MISSING_YAML = (
    "--config needs PyYAML, which is not installed. Install it with: pip install 'cliffracer[cli]'"
)


_VALID_FIELDS = set(ServiceConfig.model_fields.keys())


class ConfigError(Exception):
    """Raised when a --config file is malformed or references unknown fields."""


def _yaml() -> types.ModuleType:
    try:
        import yaml
    except ImportError as e:
        raise ConfigError(_MISSING_YAML) from e
    return yaml


def _validate_fields(section: dict[str, Any], where: str) -> None:
    for key in section:
        if key not in _VALID_FIELDS:
            raise ConfigError(f"unknown ServiceConfig field '{key}' in {where}")


def load_yaml_config(path: str | None) -> dict[str, Any]:
    """Load a --config YAML file into ``{'global': {...}, 'services': {name: {...}}}``."""
    if path is None:
        return {"global": {}, "services": {}}
    yaml = _yaml()
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"could not read --config file '{path}': {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"--config must contain a mapping, got {type(raw).__name__}")

    global_section = raw.get("global") or {}
    if not isinstance(global_section, dict):
        raise ConfigError(f"global must be a mapping, got {type(global_section).__name__}")

    services_section = raw.get("services", {}) or {}
    if not isinstance(services_section, dict):
        raise ConfigError(f"services must be a mapping, got {type(services_section).__name__}")

    _validate_fields(global_section, "global")
    for name, section in services_section.items():
        if not isinstance(section, dict):
            raise ConfigError(f"services.{name} must be a mapping, got {type(section).__name__}")
        _validate_fields(section, f"services.{name}")
    return {"global": global_section, "services": services_section}


def build_overrides(
    service_name: str, yaml_config: dict[str, Any], flag_overrides: dict[str, Any]
) -> dict[str, Any]:
    """Merge global < per-service < CLI flags into one overlay dict for a service."""
    merged: dict[str, Any] = {}
    merged.update(yaml_config.get("global", {}))
    merged.update(yaml_config.get("services", {}).get(service_name, {}) or {})
    merged.update(flag_overrides)
    return merged
