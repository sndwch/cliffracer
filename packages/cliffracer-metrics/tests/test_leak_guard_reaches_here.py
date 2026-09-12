"""Verify the leaked task guard fixture applies to package tests."""

import pytest

pytestmark = pytest.mark.unit


GUARD = "_no_leaked_tasks"


def test_the_leaked_task_guard_applies_to_package_tests(request):
    assert GUARD in request.fixturenames, f"{GUARD} must be active for packages/"


def test_CONTROL_the_check_reads_the_real_fixture_list(request):
    """Verify request.fixturenames distinguishes present and absent fixtures."""
    assert "_not_a_fixture_anybody_defined" not in request.fixturenames
    assert "request" in request.fixturenames
