import pytest

from cliffracer.cli.main import build_orchestrator, build_parser, flag_overrides_from_args
from tests.unit.cli_fixtures.sample_services import AlphaService

MOD = "tests.unit.cli_fixtures.sample_services"


@pytest.mark.unit
def test_parser_run_accepts_targets_and_flags():
    parser = build_parser()
    args = parser.parse_args(
        ["run", f"{MOD}:AlphaService", "--nats-url", "nats://x:4222", "--log-level", "DEBUG"]
    )
    assert args.command == "run"
    assert args.targets == [f"{MOD}:AlphaService"]
    assert args.nats_url == "nats://x:4222"
    assert args.log_level == "DEBUG"


@pytest.mark.unit
def test_flag_overrides_only_includes_set_flags():
    parser = build_parser()
    args = parser.parse_args(["run", f"{MOD}:AlphaService"])
    assert flag_overrides_from_args(args) == {}


@pytest.mark.unit
def test_build_orchestrator_resolves_targets_and_carries_overrides():
    """What the two removed backdoor tests also proved, minus the backdoor.

    They were the ONLY callers of build_orchestrator; deleting them for the
    `--backdoor-port` removal would have left it untested, and `tests/` is not
    linted by CI, so the orphaned imports would not have said so either.
    """
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


@pytest.mark.unit
def test_build_orchestrator_expands_a_module_target_to_every_service():
    orch = build_orchestrator([MOD], nats_url=None, log_level=None, config_path=None)
    assert len(orch.runners) == 2
