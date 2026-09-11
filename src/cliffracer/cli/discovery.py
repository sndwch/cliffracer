"""Resolve CLI targets (``module:Class`` or bare ``module``) into service classes."""

import importlib
import inspect
import types
from typing import Any

from cliffracer.core import CliffracerService


class DiscoveryError(Exception):
    """Raised when a CLI target cannot be resolved to a runnable service."""


def _import_module(module_path: str) -> types.ModuleType:
    try:
        return importlib.import_module(module_path)
    except ImportError as e:
        raise DiscoveryError(f"could not import module '{module_path}': {e}") from e


def _is_own_service(obj: Any, module: types.ModuleType) -> bool:
    return (
        inspect.isclass(obj)
        and issubclass(obj, CliffracerService)
        and obj is not CliffracerService
        and getattr(obj, "__module__", None) == module.__name__
    )


def _resolve_one(target: str) -> list[type]:
    if ":" in target:
        module_path, _, class_name = target.partition(":")
        module = _import_module(module_path)
        if not hasattr(module, class_name):
            raise DiscoveryError(f"module '{module_path}' has no attribute '{class_name}'")
        obj = getattr(module, class_name)
        if not (inspect.isclass(obj) and issubclass(obj, CliffracerService)):
            raise DiscoveryError(f"'{target}' is not a Cliffracer service class")
        return [obj]

    module = _import_module(target)
    found = [obj for _, obj in inspect.getmembers(module) if _is_own_service(obj, module)]
    if not found:
        raise DiscoveryError(f"no Cliffracer services found in module '{target}'")
    return found


def resolve_targets(targets: list[str]) -> list[type]:
    """Resolve targets into an order-preserving, de-duplicated list of service classes."""
    resolved: list[type] = []
    for target in targets:
        for cls in _resolve_one(target):
            if cls not in resolved:
                resolved.append(cls)
    return resolved
