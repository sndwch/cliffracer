"""A config overlay is validated as one config, so a pair of settings that is valid together can be set.

`apply_config_overlay` assigned each field with `setattr`, and `ServiceConfig` revalidates the whole
model on every assignment, model validators included. `nats_user` and `nats_password` must be set
together, so the first of the pair was refused whichever order it was written in: a `--config` YAML,
`ServiceRunner(..., overrides=...)`, the test harness and `construct_service(Svc, config)` could never
carry a username and a password onto a self-configuring service, although the same config is valid on
its own. A refusal part way through also left the fields before it changed.
"""

import pytest
from pydantic import SecretStr, ValidationError

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.construction import apply_config_overlay, construct_service
from cliffracer.runners.orchestrator import ServiceRunner

pytestmark = pytest.mark.unit


class Own(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="own", health_port=0))


class Bound(CliffracerService):
    """A service whose own config has updates on, which a bind-mode overlay has to turn off."""

    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="bound", health_port=0, jetstream_update_streams=True))


def _credentials(config: ServiceConfig) -> dict:
    return config.nats_auth_kwargs()


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"nats_user": "ops", "nats_password": "pw"}, id="user-then-password"),
        pytest.param({"nats_password": "pw", "nats_user": "ops"}, id="password-then-user"),
    ],
)
def test_a_user_and_password_are_overlaid_together_in_either_order(values):
    service = Own()

    apply_config_overlay(service, values)

    assert _credentials(service.config) == {"user": "ops", "password": "pw"}


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(
            {"jetstream_resource_mode": "bind", "jetstream_update_streams": False}, id="mode-first"
        ),
        pytest.param(
            {"jetstream_update_streams": False, "jetstream_resource_mode": "bind"},
            id="updates-first",
        ),
    ],
)
def test_bind_mode_and_updates_off_are_overlaid_together_in_either_order(values):
    service = Bound()
    assert service.config.jetstream_update_streams is True

    apply_config_overlay(service, values)

    assert service.config.jetstream_resource_mode == "bind"
    assert service.config.jetstream_update_streams is False


@pytest.mark.parametrize(
    ("service_class", "values", "says"),
    [
        pytest.param(
            Bound,
            {"jetstream_resource_mode": "bind"},
            "jetstream_update_streams cannot be enabled",
            id="bind-over-a-config-with-updates-on",
        ),
        pytest.param(
            Own,
            {"nats_user": "ops"},
            "nats_user and nats_password go together",
            id="a-user-over-a-config-with-no-password",
        ),
    ],
)
def test_the_overlay_is_checked_against_the_config_it_lands_on_not_alone(
    service_class, values, says
):
    service = service_class()
    before = service.config.model_dump()

    with pytest.raises(ValidationError, match=says):
        apply_config_overlay(service, values)

    assert service.config.model_dump() == before


def test_construct_service_carries_a_config_valid_on_its_own_onto_a_self_configuring_service():
    config = ServiceConfig(name="own-name", nats_user="ops", nats_password="pw")

    service = construct_service(Own, config)

    assert _credentials(service.config) == {"user": "ops", "password": "pw"}
    assert service.config.name == "own", "the service keeps its own name"


def test_a_runner_given_overrides_with_both_credentials_builds_the_service():
    runner = ServiceRunner(Own, overrides={"nats_user": "ops", "nats_password": "pw"})

    service = runner._construct_service()

    assert _credentials(service.config) == {"user": "ops", "password": "pw"}


def test_the_config_is_changed_in_place_so_everything_holding_it_sees_the_overlay():
    service = Own()
    before = service.config
    held_by_the_container = service.container.config

    apply_config_overlay(service, {"nats_user": "ops", "nats_password": "pw"})

    assert service.config is before and held_by_the_container is before
    assert _credentials(held_by_the_container) == {"user": "ops", "password": "pw"}


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(
            {"health_port": 9090, "nats_user": "ops"}, id="a-half-pair-after-a-good-field"
        ),
        pytest.param(
            {"health_port": 9090, "max_event_concurrency": -1}, id="a-bad-value-after-a-good"
        ),
        pytest.param({"nats_user": "ops", "nats_token": "t"}, id="two-ways-to-authenticate"),
    ],
)
def test_a_refused_overlay_changes_nothing(values):
    service = Own()
    before = service.config.model_dump()
    set_before = set(service.config.model_fields_set)

    with pytest.raises(ValidationError):
        apply_config_overlay(service, values)

    assert service.config.model_dump() == before
    assert service.config.model_fields_set == set_before


def test_an_overlaid_field_counts_as_set():
    service = Own()
    set_before = set(service.config.model_fields_set)
    assert "max_event_concurrency" not in set_before

    apply_config_overlay(service, {"max_event_concurrency": 4})

    assert service.config.model_fields_set == set_before | {"max_event_concurrency"}
    assert service.config.max_event_concurrency == 4


def test_an_overlaid_value_is_the_validated_one():
    """What is written is the validated value, not the caller's raw input: a `SecretStr` for the
    password and an `int` for a port given as text."""
    service = Own()

    apply_config_overlay(
        service, {"nats_password": "pw", "nats_user": "ops", "health_port": "9090"}
    )

    assert service.config.nats_user == "ops"
    assert _credentials(service.config)["password"] == "pw"
    assert isinstance(service.config.nats_password, SecretStr)
    assert service.config.health_port == 9090 and type(service.config.health_port) is int


def test_CONTROL_a_single_field_overlay_still_works_and_leaves_the_others_alone():
    service = Own()
    service.config.nats_token = "t"  # type: ignore[assignment]

    apply_config_overlay(service, {"health_port": 9091})

    assert service.config.health_port == 9091
    assert _credentials(service.config) == {"token": "t"}
    assert service.config.name == "own"


def test_CONTROL_an_unknown_field_is_still_named_and_refused():
    service = Own()

    with pytest.raises(
        ValueError, match="Unknown ServiceConfig field in overrides: 'no_such_field'"
    ):
        apply_config_overlay(service, {"health_port": 9090, "no_such_field": 1})

    assert service.config.health_port == 0, "nothing was applied"


def test_CONTROL_an_overlay_that_names_nothing_changes_nothing():
    service = Own()
    before = service.config.model_dump()

    apply_config_overlay(service, {})

    assert service.config.model_dump() == before
