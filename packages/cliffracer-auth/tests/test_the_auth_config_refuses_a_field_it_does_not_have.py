"""`AuthConfig` refuses an option it does not have, naming it.

Pydantic ignores unknown keys unless told otherwise, so `AuthConfig(pbkdf2_iteration=5)`, one
letter short of the real field, built without a word and left the operator running at the
default hash cost. `ServiceConfig` forbids extras for exactly this reason. The test that proved a
removed field was gone checked the schema, which stays true for a caller still passing it.
"""

import pytest
from cliffracer_auth import AuthConfig, SimpleAuthService
from pydantic import ValidationError

pytestmark = pytest.mark.unit

SECRET = "x" * 40


@pytest.mark.parametrize(
    "unknown",
    [{"pbkdf2_iteration": 5}, {"bcrypt_rounds": 12}, {"token_expiry": 1}, {"anything": True}],
)
def test_an_unknown_field_is_refused_and_named(unknown):
    (name,) = unknown

    with pytest.raises(ValidationError) as caught:
        AuthConfig(secret_key=SECRET, **unknown)

    assert name in str(caught.value)
    assert "Extra inputs are not permitted" in str(caught.value)


@pytest.mark.parametrize("value", [True, False])
def test_the_removed_enable_auth_is_refused_with_what_to_do_instead(value):
    with pytest.raises(ValidationError) as caught:
        AuthConfig(secret_key=SECRET, enable_auth=value)

    message = str(caught.value)
    assert "enable_auth" in message and "AuthExtension" in message


def test_a_misspelt_option_does_not_leave_the_default_in_place():
    """The failure this exists for: the typo must not construct a config at the default cost."""
    with pytest.raises(ValidationError):
        AuthConfig(secret_key=SECRET, pbkdf2_iteration=1_000_000)


def test_CONTROL_every_real_field_is_accepted():
    config = AuthConfig(
        secret_key=SECRET,
        algorithm="HS384",
        token_expiry_hours=2,
        pbkdf2_iterations=2_000,
        refresh_max_lifetime_hours=48,
    )

    assert (config.algorithm, config.token_expiry_hours, config.pbkdf2_iterations) == (
        "HS384",
        2,
        2_000,
    )
    assert config.refresh_max_lifetime_hours == 48
    SimpleAuthService(config)


def test_CONTROL_the_defaults_still_build():
    assert AuthConfig(secret_key=SECRET).pbkdf2_iterations == 100_000
