import pytest
from loguru import logger

from cliffracer.cli.main import _flag_overrides, build_orchestrator, build_parser, main
from tests.unit.cli_fixtures.sample_services import AlphaService

pytestmark = pytest.mark.unit

MOD = "tests.unit.cli_fixtures.sample_services"


def test_parser_run_accepts_targets_and_flags():
    parser = build_parser()
    args = parser.parse_args(
        ["run", f"{MOD}:AlphaService", "--nats-url", "nats://x:4222", "--log-level", "DEBUG"]
    )
    assert args.command == "run"
    assert args.targets == [f"{MOD}:AlphaService"]
    assert args.nats_url == "nats://x:4222"
    assert args.log_level == "DEBUG"


def test_flag_overrides_only_includes_set_flags():
    parser = build_parser()
    args = parser.parse_args(["run", f"{MOD}:AlphaService"])
    assert _flag_overrides(args.nats_url) == {}


def test_a_set_flag_is_in_the_overrides():
    """The other direction: a helper that returned {} for everything passed the test above."""
    parser = build_parser()
    args = parser.parse_args(["run", f"{MOD}:AlphaService", "--nats-url", "nats://x:4222"])

    assert _flag_overrides(args.nats_url) == {"nats_url": "nats://x:4222"}


def test_the_parser_maps_config_to_config_path():
    parser = build_parser()

    assert parser.parse_args(["run", f"{MOD}:AlphaService", "--config", "d.yaml"]).config_path == (
        "d.yaml"
    )
    assert parser.parse_args(["run", f"{MOD}:AlphaService"]).config_path is None


def test_run_exits_2_for_a_config_file_that_is_not_a_mapping(error_records, tmp_path):
    """A ConfigError is reported like a DiscoveryError: exit code 2 and a logged reason."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- list\n")

    assert main(["run", f"{MOD}:AlphaService", "--config", str(bad)]) == 2

    (record,) = error_records
    assert "--config must contain a mapping" in record["message"]


def test_build_orchestrator_resolves_targets_and_carries_overrides():
    """`build_orchestrator` resolves each target to a runner and carries the overrides."""
    orch = build_orchestrator(
        [f"{MOD}:AlphaService"],
        nats_url="nats://x:4222",
        log_level=None,
        config_path=None,
    )
    assert len(orch.runners) == 1
    runner = orch.runners[0]
    assert runner.service_class is AlphaService
    assert runner.overrides["nats_url"] == "nats://x:4222"


def test_build_orchestrator_expands_a_module_target_to_every_service():
    orch = build_orchestrator([MOD], nats_url=None, log_level=None, config_path=None)
    assert len(orch.runners) == 2


@pytest.fixture
def error_records():
    records: list = []
    sink = logger.add(lambda message: records.append(message.record), level="ERROR")
    yield records
    logger.remove(sink)


@pytest.mark.parametrize(
    ("module", "cause"),
    [
        ("tests.unit.cli_fixtures.raises_at_import", ValueError),
        ("tests.unit.cli_fixtures.broken_import", ImportError),
    ],
    ids=["raises_at_import", "CONTROL_broken_import"],
)
def test_run_exits_2_for_a_module_that_does_not_import_and_logs_why(error_records, module, cause):
    """The exit code is what `main` returns, read directly, and the log carries
    the cause's traceback rather than only its message."""
    assert main(["run", module]) == 2

    (record,) = error_records
    assert "could not import" in record["message"]
    assert record["exception"] is not None and record["exception"].type is not None
    assert issubclass(record["exception"].type, cause), record["exception"]


def test_run_exits_2_for_a_service_whose_constructor_raises_and_logs_why(error_records):
    """The orchestrator builds each class once to read its name. A constructor
    that raises is reported like a module that cannot import, not as a traceback."""
    assert main(["run", "tests.unit.cli_fixtures.constructor_raises:ConstructorRaises"]) == 2

    (record,) = error_records
    assert "could not construct service" in record["message"]
    assert "ConstructorRaises" in record["message"]
    assert "RuntimeError: config missing: DATABASE_URL" in record["message"]
    assert record["exception"] is not None and record["exception"].type is RuntimeError


def test_CONTROL_a_service_that_constructs_still_builds():
    orch = build_orchestrator(
        [f"{MOD}:AlphaService"], nats_url=None, log_level=None, config_path=None
    )

    assert len(orch.runners) == 1
