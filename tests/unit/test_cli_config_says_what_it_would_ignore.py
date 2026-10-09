"""`cliffracer run --config` refuses a key it would drop, and the README's example is one it applies.

The file has two sections, `global:` and `services:` (a mapping from service name to settings). A key
outside them, and a section under `services:` for a name no service in the run has, used to be
dropped without a word, so a service section written at the top level, as the README once showed it,
applied nothing: the credentials in the example were never set.
"""

import re
from pathlib import Path

import pytest
from loguru import logger

from cliffracer.cli.config import ConfigError, build_overrides, load_yaml_config
from cliffracer.cli.main import build_orchestrator

pytestmark = pytest.mark.unit

README = Path(__file__).resolve().parents[2] / "README.md"
MOD = "tests.unit.cli_fixtures.sample_services"


def _write(tmp_path, text: str) -> str:
    path = tmp_path / "deploy.yaml"
    path.write_text(text)
    return str(path)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("my_service:\n  nats_user: svc\n", "my_service"),
        ("service:\n  my_service:\n    nats_user: svc\n", "service"),
        ("globals:\n  nats_user: svc\n", "globals"),
    ],
    ids=["a-service-section-at-the-top", "singular-services", "typo-for-global"],
)
def test_a_top_level_key_that_is_neither_global_nor_services_is_refused(tmp_path, text, key):
    with pytest.raises(ConfigError) as caught:
        load_yaml_config(_write(tmp_path, text))

    message = str(caught.value)
    assert f"'{key}'" in message
    assert "'global'" in message and "'services'" in message
    assert "services.<name>" in message or "under services" in message


def test_the_readmes_deploy_yaml_example_applies_its_settings(tmp_path):
    readme = README.read_text()
    (block,) = [b for b in re.findall(r"```yaml\n(.*?)```", readme, re.S) if "# deploy.yaml" in b]

    overrides = build_overrides("my_service", load_yaml_config(_write(tmp_path, block)), {})

    assert overrides["nats_user"] == "svc"
    assert overrides["nats_password"] == "s3cret"
    assert overrides["nats_url"] == "nats://broker:4222"


def test_CONTROL_global_and_services_sections_still_apply(tmp_path):
    config = load_yaml_config(
        _write(
            tmp_path, "global:\n  nats_user: g\nservices:\n  alpha_service:\n    version: '2.0.0'\n"
        )
    )

    assert build_overrides("alpha_service", config, {}) == {"nats_user": "g", "version": "2.0.0"}


def test_CONTROL_an_empty_file_and_a_global_only_file_are_accepted(tmp_path):
    assert load_yaml_config(_write(tmp_path, "")) == {"global": {}, "services": {}}
    assert load_yaml_config(_write(tmp_path, "global:\n  nats_user: g\n"))["global"] == {
        "nats_user": "g"
    }


@pytest.fixture
def warnings():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    yield lines
    logger.remove(sink)


def test_a_services_section_for_a_name_no_service_in_the_run_has_is_reported(tmp_path, warnings):
    path = _write(tmp_path, "services:\n  alpha_servce:\n    version: '2.0.0'\n")

    build_orchestrator([f"{MOD}:AlphaService"], nats_url=None, log_level=None, config_path=path)

    (line,) = [w for w in warnings if "alpha_servce" in w]
    assert "alpha_service" in line, (
        "the line names the services that are run, so the typo is findable"
    )


def test_CONTROL_a_services_section_for_a_service_in_the_run_is_not_reported(tmp_path, warnings):
    path = _write(tmp_path, "services:\n  alpha_service:\n    version: '2.0.0'\n")

    build_orchestrator([f"{MOD}:AlphaService"], nats_url=None, log_level=None, config_path=path)

    assert not [w for w in warnings if "--config" in w]
