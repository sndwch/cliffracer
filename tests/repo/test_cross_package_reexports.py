"""Tests verifying core modules maintain required re-exports for external packages."""

import importlib

import pytest

pytestmark = pytest.mark.repo

CROSS_PACKAGE_REEXPORTS = [
    ("cliffracer.core.service", "redact_nats_url", "cliffracer-backdoor"),
]


@pytest.mark.parametrize("module, name, consumer", CROSS_PACKAGE_REEXPORTS)
def test_a_name_another_distribution_imports_stays_importable(module, name, consumer):
    mod = importlib.import_module(module)
    assert hasattr(mod, name), f"{module}.{name} is missing, required by {consumer}"
