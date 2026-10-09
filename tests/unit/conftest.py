"""Fixtures shared by the unit tests."""

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def mypy_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One mypy cache for every strict check in this pytest invocation.

    The checks run with the same options and read the same dependencies, so each one after the
    first reuses that analysis. Each check writes its sources to fresh paths, which mypy validates
    by content; `test_generated_client_strict_typing` plants an equal-size, equal-timestamp change
    through this cache and requires it to be seen.
    """
    return tmp_path_factory.mktemp("mypy-cache")
