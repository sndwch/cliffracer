"""A config overlay the service config refuses ends `cliffracer run`, and nothing prints its secrets.

`--config` YAML and the flags are laid over each service's own `ServiceConfig` as one overlay, and
the merged config is validated as a whole, so a wrong value or an inconsistent pair (`nats_user`
without `nats_password`) is refused at construction. The runner treated that refusal as a crash and
retried it on the restart backoff, but the overlay is the same on every attempt, so no attempt can
succeed: `cliffracer run` never exited. Each retry also logged a traceback, and with loguru's
`diagnose=True` a traceback prints the value of every expression on its frame lines, here the
overlay dict with the YAML password in it.

A refusal found before the runner starts is `ConfigError`, exit 2, as an unknown key in the same
file is. A programmatic runner given the same overlay reports it once, without a traceback, and
stops with `RUNNER_SERVICE_DOWN`. The sinks read here set `diagnose=True`: the shape of loguru's own
default sink, which `cliffracer run` keeps when `--log-level` is left off.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from loguru import logger
from pydantic import Field, SecretStr

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.cli.config import ConfigError
from cliffracer.cli.main import build_orchestrator, main
from cliffracer.runners.orchestrator import (
    RUNNER_SERVICE_DOWN,
    ServiceOrchestrator,
    ServiceRunner,
    _OverlayRefused,
)
from tests.unit.cli_fixtures.sample_services import AlphaService

pytestmark = pytest.mark.unit

MOD = "tests.unit.cli_fixtures.sample_services"
CANARY = "SECRETPW-CANARY-8d1f"

# Each is refused by the merged config: one by value, one by the pair.
REFUSED = [
    pytest.param(
        f"global:\n  nats_user: alice\n  nats_password: {CANARY}\n  restart_delay: -1\n",
        "restart_delay",
        id="a-value-out-of-range",
    ),
    pytest.param(
        f"global:\n  nats_user: alice\n  nats_password: {CANARY}\n  nats_token: {CANARY}\n",
        "nats_token",
        id="two-credentials-at-once",
    ),
]


@pytest.fixture
def written(tmp_path: Path):
    def write(text: str) -> str:
        path = tmp_path / "deploy.yaml"
        path.write_text(text)
        return str(path)

    return write


@pytest.fixture
def sink():
    """Everything loguru writes, from a sink configured as loguru's default is."""
    texts: list[str] = []
    handler = logger.add(lambda message: texts.append(str(message)), level="DEBUG", diagnose=True)
    try:
        yield texts
    finally:
        logger.remove(handler)


@pytest.mark.parametrize(("yaml_text", "names"), REFUSED)
def test_the_cli_refuses_the_overlay_before_the_runner_starts(written, yaml_text, names):
    with pytest.raises(ConfigError) as caught:
        build_orchestrator(
            [f"{MOD}:AlphaService"], nats_url=None, log_level=None, config_path=written(yaml_text)
        )

    text = str(caught.value)
    assert "alpha_service" in text and names in text, text
    assert CANARY not in text and CANARY not in repr(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(("yaml_text", "names"), REFUSED)
def test_cliffracer_run_exits_2_and_prints_no_secret(monkeypatch, written, sink, yaml_text, names):
    def never_started(self) -> None:
        raise AssertionError("a run was started for a refused overlay")

    monkeypatch.setattr(ServiceOrchestrator, "run_forever", never_started)

    code = main(["run", f"{MOD}:AlphaService", "--config", written(yaml_text)])

    assert code == 2
    output = "".join(sink)
    assert names in output, output
    assert CANARY not in output


def test_CONTROL_the_cli_still_refuses_an_unknown_key_with_exit_2(monkeypatch, written, sink):
    monkeypatch.setattr(ServiceOrchestrator, "run_forever", lambda self: None)

    assert main(["run", f"{MOD}:AlphaService", "--config", written("global:\n  bogus: 1\n")]) == 2

    assert "bogus" in "".join(sink)


def test_CONTROL_a_valid_overlay_is_built_and_carried_to_the_runner(written):
    orchestrator = build_orchestrator(
        [f"{MOD}:AlphaService"],
        nats_url=None,
        log_level=None,
        config_path=written(
            "global:\n  nats_user: alice\n  nats_password: pw\n  restart_delay: 3\n"
        ),
    )

    (runner,) = orchestrator.runners
    assert runner.overrides == {"nats_user": "alice", "nats_password": "pw", "restart_delay": 3}


async def _run_refused_overlay(monkeypatch, overrides) -> tuple[int | None, int]:
    constructions = 0
    real = ServiceRunner._construct_service

    def counted(self):
        nonlocal constructions
        constructions += 1
        return real(self)

    monkeypatch.setattr(ServiceRunner, "_construct_service", counted)
    runner = ServiceRunner(AlphaService, overrides=overrides)
    try:
        status = await asyncio.wait_for(runner.run(), timeout=3)
    except TimeoutError:
        status = None
    finally:
        runner._shutdown_event.set()
    return status, constructions


async def test_a_runner_given_a_refused_overlay_constructs_once_and_reports_the_service_down(
    monkeypatch, sink
):
    status, constructions = await _run_refused_overlay(
        monkeypatch, {"nats_password": CANARY, "restart_delay": -1}
    )

    assert status == RUNNER_SERVICE_DOWN
    assert constructions == 1
    output = "".join(sink)
    assert "restart_delay" in output and "Not retrying" in output, output
    assert CANARY not in output


async def test_a_runner_given_a_refused_overlay_logs_no_traceback(monkeypatch, sink):
    await _run_refused_overlay(monkeypatch, {"nats_password": CANARY, "restart_delay": -1})

    assert "Traceback" not in "".join(sink)


class KeyConfig(ServiceConfig):
    api_key: SecretStr | None = Field(default=None, min_length=32)


class KeyService(CliffracerService):
    def __init__(self) -> None:
        super().__init__(KeyConfig(name="key_service"))


def test_a_refused_credential_a_config_subclass_declares_is_named_and_not_shown():
    """The refusal is built from each error's location and message, so it names the field and
    carries none of the value refused, for a field the config subclass declares too."""
    runner = ServiceRunner(KeyService, overrides={"api_key": CANARY})

    with pytest.raises(_OverlayRefused) as caught:
        runner._construct_service()

    text = str(caught.value)
    assert text.startswith("api_key: "), text
    assert CANARY not in text


async def test_an_overlay_with_an_unknown_key_beside_a_credential_stops_the_run_once(
    monkeypatch, sink
):
    status, constructions = await _run_refused_overlay(
        monkeypatch, {"nats_password": CANARY, "bogus": 1}
    )

    assert status == RUNNER_SERVICE_DOWN and constructions == 1
    output = "".join(sink)
    assert "bogus" in output and "Not retrying" in output, output
    assert "Traceback" not in output and CANARY not in output


def test_the_runners_refusal_is_not_chained_to_the_error_that_holds_the_overlay():
    """The refused-overlay error is built from fields and reasons; the original's frames are not kept."""
    runner = ServiceRunner(AlphaService, overrides={"nats_password": CANARY, "restart_delay": -1})

    with pytest.raises(_OverlayRefused) as caught:
        runner._construct_service()

    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert CANARY not in str(caught.value)


def test_CONTROL_a_constructor_that_raises_a_value_error_is_not_taken_for_a_refused_overlay():
    """Only the overlay is constant: what a constructor raises propagates, and the loop retries it."""

    class RaisesInItsConstructor(AlphaService):
        def __init__(self):
            raise ValueError("the constructor's own")

    runner = ServiceRunner(RaisesInItsConstructor, overrides={"restart_delay": 3})

    with pytest.raises(ValueError, match="the constructor's own"):
        runner._construct_or_give_up()


def test_CONTROL_a_valid_overlay_is_applied_by_the_runner():
    runner = ServiceRunner(AlphaService, overrides={"restart_delay": 3})

    service = runner._construct_or_give_up()

    assert service is not None and service.config.restart_delay == 3


def test_the_sink_the_orchestrator_installs_for_a_log_level_shows_no_values(capsys):
    """`--log-level` replaces the process's sinks; the one it adds prints frames, not values. A
    sink with `diagnose=True`, the shape of loguru's default, is installed first and must be gone
    afterwards: it would print the value."""
    secret = CANARY
    texts: list[str] = []

    def fails() -> None:
        password = secret
        raise RuntimeError(len(password))

    try:
        logger.remove()
        logger.add(lambda message: texts.append(str(message)), level="DEBUG", diagnose=True)
        ServiceOrchestrator(log_level="DEBUG")._configure_logging()
        try:
            fails()
        except RuntimeError:
            logger.exception("crashed")
    finally:
        logger.remove()
        logger.add(sys.__stderr__)

    err = capsys.readouterr().err
    assert "crashed" in err and "fails" in err
    assert CANARY not in err
    assert CANARY not in "".join(texts), "the sink installed before --log-level was kept"


def test_CONTROL_a_sink_with_diagnose_on_does_print_a_local_variable(sink):
    """The instrument can fail: this sink shows the value the test above looks for."""

    def fails() -> None:
        password = CANARY
        raise RuntimeError(len(password))

    try:
        fails()
    except RuntimeError:
        logger.exception("crashed")

    assert CANARY in "".join(sink)
