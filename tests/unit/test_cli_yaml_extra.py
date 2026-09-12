"""Tests verifying PyYAML optional dependency behavior for CLI configuration loading."""

import sys

import pytest

from cliffracer.cli.config import ConfigError, load_yaml_config

pytestmark = pytest.mark.unit


@pytest.fixture
def no_pyyaml(monkeypatch):
    """Simulate PyYAML absence in sys.modules."""
    monkeypatch.delitem(sys.modules, "yaml", raising=False)
    monkeypatch.setitem(sys.modules, "yaml", None)


def test_no_config_flag_does_not_need_pyyaml(no_pyyaml):
    """`cliffracer run` with no --config is the path that must stay dependency-free."""
    assert load_yaml_config(None) == {"global": {}, "services": {}}


def test_the_cli_MODULE_ITSELF_imports_without_pyyaml(no_pyyaml):
    """Verify cliffracer.cli.config module imports when PyYAML is not installed."""
    import importlib.util
    from pathlib import Path

    import cliffracer.cli.config as already

    path = Path(already.__file__)
    spec = importlib.util.spec_from_file_location("cliffracer_cli_config_fresh", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # must not raise

    assert module.load_yaml_config(None) == {"global": {}, "services": {}}


def test_config_without_pyyaml_names_the_extra(no_pyyaml, tmp_path):
    path = tmp_path / "services.yaml"
    path.write_text("global:\n  nats_url: nats://x:4222\n")

    with pytest.raises(ConfigError) as exc:
        load_yaml_config(str(path))

    message = str(exc.value)
    assert "PyYAML" in message
    assert "cliffracer[cli]" in message, message
    # Verify error message is a single line.
    assert "\n" not in message, message


def test_CONTROL_with_pyyaml_present_the_same_file_loads():
    """Verify configuration loads successfully when PyYAML is available."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "services.yaml"
        path.write_text("global:\n  nats_url: nats://x:4222\n")
        loaded = load_yaml_config(str(path))

    assert loaded == {"global": {"nats_url": "nats://x:4222"}, "services": {}}


def test_core_declares_only_the_three_spec_dependencies():
    """Verify core dependencies and cli optional dependencies in pyproject.toml."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    meta = tomllib.loads((root / "pyproject.toml").read_text())

    declared = {
        d.split(">")[0].split("=")[0].split("[")[0].strip() for d in meta["project"]["dependencies"]
    }
    assert declared == {"nats-py", "pydantic", "loguru"}, declared
    assert any("PyYAML" in d for d in meta["project"]["optional-dependencies"]["cli"])
    assert any("msgpack" in d for d in meta["project"]["optional-dependencies"]["msgpack"])


def test_msgpack_absence_raises_actionable_import_error(monkeypatch):
    """When msgpack is absent, attempting to pack or unpack raises actionable error."""
    import cliffracer.core.validation as val

    monkeypatch.setattr(val, "msgpack", None)

    with pytest.raises(ImportError) as exc:
        val.pack_msgpack({"a": 1})
    assert "cliffracer[msgpack]" in str(exc.value)

    with pytest.raises(ImportError) as exc:
        val.unpack_msgpack(b"dummy")
    assert "cliffracer[msgpack]" in str(exc.value)
