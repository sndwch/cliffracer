"""A proxy ``call_async`` sends nothing until awaited, and the type checker says so."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CALLER = """from cliffracer import CliffracerService, RpcProxy


class Caller(CliffracerService):
    other = RpcProxy("other_service")

    async def notify(self) -> None:
        {statement}
"""


def _mypy(tmp_path: Path, statement: str, cache: Path) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "caller.py"
    source.write_text(CALLER.format(statement=statement))
    config = tmp_path / "mypy.ini"
    config.write_text("[mypy]\n")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(config),
            "--strict",
            "--cache-dir",
            str(cache),
            "--follow-imports=silent",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_an_awaited_proxy_call_async_type_checks(tmp_path: Path, mypy_cache: Path) -> None:
    result = _mypy(tmp_path, "await self.other.record.call_async(item='widget')", mypy_cache)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_dropped_proxy_call_async_is_reported_as_an_unused_coroutine(
    tmp_path: Path, mypy_cache: Path
) -> None:
    result = _mypy(tmp_path, "self.other.record.call_async(item='widget')", mypy_cache)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "[unused-coroutine]" in result.stdout, result.stdout
    assert "caller.py:8" in result.stdout, result.stdout
