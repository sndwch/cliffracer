import pytest

from cliffracer.cli.config import ConfigError, build_overrides, load_yaml_config

pytestmark = pytest.mark.unit


def test_load_yaml_none_returns_empty_sections():
    cfg = load_yaml_config(None)
    assert cfg == {"global": {}, "services": {}}


def test_load_yaml_parses_sections(tmp_path):
    p = tmp_path / "deploy.yaml"
    p.write_text(
        "global:\n  nats_url: nats://g:4222\nservices:\n  alpha_service:\n    auto_restart: false\n"
    )
    cfg = load_yaml_config(str(p))
    assert cfg["global"]["nats_url"] == "nats://g:4222"
    assert cfg["services"]["alpha_service"]["auto_restart"] is False


def test_load_yaml_unknown_field_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("global:\n  bogus_field: 1\n")
    with pytest.raises(ConfigError, match="bogus_field"):
        load_yaml_config(str(p))


def test_build_overrides_precedence():
    yaml_config = {
        "global": {"nats_url": "nats://g:4222", "log_level": "INFO"},
        "services": {"alpha_service": {"log_level": "WARNING"}},
    }
    flags = {"log_level": "DEBUG"}  # flags win
    result = build_overrides("alpha_service", yaml_config, flags)
    assert result["nats_url"] == "nats://g:4222"  # from global
    assert result["log_level"] == "DEBUG"  # flag beats per-service beats global


def test_build_overrides_no_sources_is_empty():
    assert build_overrides("alpha_service", {"global": {}, "services": {}}, {}) == {}


def test_load_yaml_unknown_field_in_service_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("services:\n  alpha_service:\n    bogus_field: 1\n")
    with pytest.raises(ConfigError, match="bogus_field"):
        load_yaml_config(str(p))


def test_load_yaml_missing_file_raises_config_error():
    with pytest.raises(ConfigError, match="could not read"):
        load_yaml_config("/nonexistent/path/does_not_exist.yaml")


def test_load_yaml_malformed_yaml_raises_config_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(": bad: yaml: [unterminated")
    with pytest.raises(ConfigError, match="could not read"):
        load_yaml_config(str(p))


def test_load_yaml_non_dict_service_section_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("services:\n  alpha_service: 42\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_yaml_config(str(p))
