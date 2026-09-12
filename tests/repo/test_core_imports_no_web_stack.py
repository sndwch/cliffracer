"""Tests verifying core library imports do not import web stack modules (fastapi, uvicorn, starlette)."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.repo

_CHECK = (
    "import sys, cliffracer; "
    "web = sorted({m.split('.')[0] for m in sys.modules "
    "if m.startswith(('fastapi', 'uvicorn', 'starlette'))}); "
    "print(','.join(web))"
)


def test_importing_cliffracer_pulls_in_no_web_stack():
    out = subprocess.run([sys.executable, "-c", _CHECK], capture_output=True, text=True, check=True)
    imported = [m for m in out.stdout.strip().split(",") if m]
    assert imported == [], f"importing cliffracer pulled in web stack modules: {imported}"


def test_CONTROL_the_check_can_see_the_web_stack_when_it_is_imported():
    """Verify check detects web stack modules when explicitly imported."""
    out = subprocess.run(
        [sys.executable, "-c", "import fastapi; " + _CHECK],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "fastapi" in out.stdout, out.stdout
