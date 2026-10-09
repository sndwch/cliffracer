"""A `ServiceConfig` does not print its broker credentials, in its repr, its dumps or its errors.

`nats_password` and `nats_token` were plain strings, so `repr(config)` and `model_dump_json()` carried
them. A refusal by a model validator, a missing field or a misspelled option made pydantic attach the
whole input to the error: `str(error)` printed a head and tail of it, and `errors()` and `json()`
carried all of it, every credential and the password embedded in `nats_url` with them. The two fields
are `SecretStr`, as `AuthConfig.secret_key` is, and a refusal is re-raised with the input hidden where
it can hold a credential.

Marker values stand in for the secrets, and each output is searched for them.
"""

import json
from typing import Any
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr, ValidationError

from cliffracer import ServiceConfig

pytestmark = pytest.mark.unit

PASSWORD, TOKEN, URL_SECRET = "PWSECRET", "TOKSECRET", "URLSECRET"
MARKERS = (PASSWORD, TOKEN, URL_SECRET)


def _leaks(text: str) -> list[str]:
    return [marker for marker in MARKERS if marker in text]


def _refusals() -> dict[str, Any]:
    """Each way a `ServiceConfig` is refused, with credentials somewhere in what was given."""

    def two_ways():
        ServiceConfig(
            name="orders",
            nats_url=f"nats://admin:{URL_SECRET}@broker.invalid",
            nats_user="ops",
            nats_password=PASSWORD,
            nats_token=TOKEN,
        )

    def user_without_password():
        ServiceConfig(name="orders", nats_user="ops", nats_token=TOKEN)

    def bind_and_update():
        ServiceConfig(
            name="orders",
            jetstream_resource_mode="bind",
            jetstream_update_streams=True,
            nats_token=TOKEN,
        )

    def dlq_template():
        ServiceConfig(
            name="orders", dlq_subject="dlq.{namespace}", nats_user="ops", nats_password=PASSWORD
        )

    def missing_name():
        ServiceConfig(nats_token=TOKEN, nats_url=f"nats://admin:{URL_SECRET}@broker.invalid")  # type: ignore[call-arg]

    def misspelled_option():
        ServiceConfig(name="orders", nats_passwrd=PASSWORD)  # type: ignore[call-arg]

    def credential_of_the_wrong_type():
        ServiceConfig(name="orders", nats_user="ops", nats_password=[PASSWORD])  # type: ignore[arg-type]

    def another_field_wrong_beside_a_credential():
        ServiceConfig(name="orders", max_event_concurrency=-7, nats_token=TOKEN)

    def assignment():
        config = ServiceConfig(
            name="orders",
            nats_user="ops",
            nats_password=PASSWORD,
            jetstream_resource_mode="bind",
        )
        config.jetstream_update_streams = True

    return {
        fn.__name__: fn
        for fn in (
            two_ways,
            user_without_password,
            bind_and_update,
            dlq_template,
            missing_name,
            misspelled_option,
            credential_of_the_wrong_type,
            another_field_wrong_beside_a_credential,
            assignment,
        )
    }


REFUSALS = _refusals()


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_no_form_of_a_refusal_carries_a_credential(case):
    with pytest.raises(ValidationError) as raised:
        REFUSALS[case]()
    error = raised.value

    assert _leaks(str(error)) == [], "str(error)"
    assert _leaks(repr(error)) == [], "repr(error)"
    assert _leaks(str(error.errors())) == [], "errors()"
    assert _leaks(error.json()) == [], "json()"


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_refusal_still_says_what_was_wrong(case):
    with pytest.raises(ValidationError) as raised:
        REFUSALS[case]()

    reasons = " ".join(detail["msg"] for detail in raised.value.errors())
    expected = {
        "two_ways": "more than one way to authenticate",
        "user_without_password": "nats_user and nats_password go together",
        "bind_and_update": "jetstream_update_streams cannot be enabled",
        "dlq_template": "dlq_subject renders an unusable subject",
        "missing_name": "Field required",
        "misspelled_option": "Extra inputs are not permitted",
        "credential_of_the_wrong_type": "Input should be a valid string",
        "another_field_wrong_beside_a_credential": "greater than 0",
        "assignment": "jetstream_update_streams cannot be enabled",
    }
    assert expected[case] in reasons


def test_a_refusal_names_the_field_and_keeps_the_value_that_belongs_to_it():
    with pytest.raises(ValidationError) as raised:
        ServiceConfig(name="orders", max_event_concurrency=-7, nats_token=TOKEN)

    (detail,) = raised.value.errors()
    assert detail["loc"] == ("max_event_concurrency",) and detail["input"] == -7
    assert "-7" in str(raised.value)


@pytest.mark.parametrize(
    "servers",
    [
        pytest.param(
            [f"nats://u:{URL_SECRET}@a.invalid", "nats://b.invalid"], id="a-list-of-servers"
        ),
        pytest.param((f"nats://u:{URL_SECRET}@a.invalid",), id="a-tuple"),
        pytest.param({"url": f"nats://u:{URL_SECRET}@a.invalid"}, id="a-mapping"),
        pytest.param([[f"nats://u:{URL_SECRET}@a.invalid"]], id="a-nested-list"),
    ],
)
def test_a_nats_url_that_is_not_a_string_is_refused_without_printing_what_it_holds(servers):
    # A YAML `nats_url` written as a list of servers reaches the model as a list.
    with pytest.raises(ValidationError) as raised:
        ServiceConfig(name="orders", nats_url=servers)  # type: ignore[arg-type]
    error = raised.value

    assert _leaks(str(error)) == [], "str(error)"
    assert _leaks(repr(error)) == [], "repr(error)"
    assert _leaks(str(error.errors())) == [], "errors()"
    assert _leaks(error.json()) == [], "json()"
    assert "Input should be a valid string" in str(error), "the refusal still says what was wrong"
    assert [detail["loc"] for detail in error.errors()] == [("nats_url",)]


def test_a_refusal_of_the_url_still_shows_it_with_the_password_hidden():
    with pytest.raises(ValidationError) as raised:
        ServiceConfig(name="orders", nats_url=f"ftp://admin:{URL_SECRET}@broker.invalid")

    text = str(raised.value) + str(raised.value.errors()) + raised.value.json()
    assert _leaks(text) == []
    assert "***@broker.invalid" in text


def _configured() -> ServiceConfig:
    return ServiceConfig(name="orders", nats_user="ops", nats_password=PASSWORD)


def test_the_repr_and_the_dumps_of_a_good_config_do_not_carry_the_password():
    config = _configured()

    assert _leaks(repr(config)) == []
    assert _leaks(str(config)) == []
    assert _leaks(str(config.model_dump())) == []
    assert _leaks(str(config.model_dump(mode="json"))) == []
    assert _leaks(config.model_dump_json()) == []
    token = ServiceConfig(name="orders", nats_token=TOKEN)
    assert _leaks(repr(token) + token.model_dump_json() + str(token.model_dump())) == []


def test_the_fields_are_secrets_that_can_be_read_deliberately():
    config = _configured()

    assert isinstance(config.nats_password, SecretStr)
    assert config.nats_password.get_secret_value() == PASSWORD
    assert config.nats_user == "ops"


def test_CONTROL_the_connection_is_made_with_the_real_values():
    assert _configured().nats_auth_kwargs() == {"user": "ops", "password": PASSWORD}
    assert ServiceConfig(name="orders", nats_token=TOKEN).nats_auth_kwargs() == {"token": TOKEN}
    assert _configured().nats_connect_kwargs() == {"user": "ops", "password": PASSWORD}


def test_CONTROL_a_config_copied_with_a_plain_string_still_connects_with_it():
    # `model_copy(update=...)` skips validation, so the field holds the string it was given.
    copied = ServiceConfig(name="orders").model_copy(
        update={"nats_user": "ops", "nats_password": PASSWORD}
    )
    token = ServiceConfig(name="orders").model_copy(update={"nats_token": TOKEN})

    assert copied.nats_auth_kwargs() == {"user": "ops", "password": PASSWORD}
    assert token.nats_connect_kwargs() == {"token": TOKEN}


def test_CONTROL_a_credential_set_by_assignment_is_the_real_value():
    config = ServiceConfig(name="orders")
    config.nats_token = TOKEN  # type: ignore[assignment]

    assert config.nats_auth_kwargs() == {"token": TOKEN}


def test_CONTROL_a_python_dump_still_round_trips_and_overlays_the_credential():
    config = _configured()

    assert ServiceConfig(**config.model_dump()).nats_auth_kwargs() == config.nats_auth_kwargs()
    overlay = ServiceConfig(name="orders", nats_token=TOKEN).model_dump(exclude_unset=True)
    assert ServiceConfig.model_validate(overlay).nats_auth_kwargs() == {"token": TOKEN}


def test_CONTROL_a_json_document_sets_the_credential_from_text():
    # A `--config` YAML file reaches the model as plain strings.
    document = json.loads('{"name": "orders", "nats_user": "ops", "nats_password": "PWSECRET"}')

    assert ServiceConfig.model_validate(document).nats_auth_kwargs() == {
        "user": "ops",
        "password": PASSWORD,
    }


# --- the password embedded in nats_url ---------------------------------------


REAL_URL = f"nats://admin:{URL_SECRET}@broker.invalid"


def _with_url(url: str = REAL_URL) -> ServiceConfig:
    return ServiceConfig(name="orders", nats_url=url)


def _outputs_of(config: ServiceConfig) -> dict[str, str]:
    return {
        "repr": repr(config),
        "str": str(config),
        "model_dump": str(config.model_dump()),
        "model_dump(mode=json)": str(config.model_dump(mode="json")),
        "model_dump_json": config.model_dump_json(),
        "__dict__": str(config.__dict__),
        "dict(config)": str(dict(config)),
        "model_copy": repr(config.model_copy()),
        "__rich_repr__": str(list(config.__rich_repr__())),
    }


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(REAL_URL, id="one-server"),
        pytest.param(f"nats://{URL_SECRET}@a.invalid", id="a-token-as-the-user"),
        pytest.param(f"tls://u:{URL_SECRET}@a.invalid", id="tls"),
        pytest.param(f"nats://a.invalid?token={URL_SECRET}", id="a-query-token"),
    ],
)
def test_the_password_in_a_nats_url_is_in_no_output_of_a_config(url):
    for output, text in _outputs_of(_with_url(url)).items():
        assert _leaks(text) == [], output


def test_the_host_of_a_nats_url_is_still_shown():
    for output, text in _outputs_of(_with_url()).items():
        assert "broker.invalid" in text, output


def test_CONTROL_the_connection_is_made_with_the_real_url():
    config = _with_url()

    assert str(config.nats_url) == REAL_URL
    assert config.nats_url == REAL_URL
    assert f"{config.nats_url}" == REAL_URL
    assert config.nats_url.startswith("nats://admin:")


def test_CONTROL_a_python_dump_still_round_trips_and_overlays_the_url():
    config = _with_url()

    assert ServiceConfig(**config.model_dump()).nats_url == REAL_URL
    assert ServiceConfig.model_validate(config.model_dump(round_trip=True)).nats_url == REAL_URL
    overlay = {**config.model_dump(exclude_unset=True), "request_timeout": 3.0}
    assert ServiceConfig.model_validate(overlay).nats_url == REAL_URL


def test_the_copy_of_a_dump_hides_the_password_as_the_original_does():
    copy = ServiceConfig.model_validate(_with_url().model_dump())

    for output, text in _outputs_of(copy).items():
        assert _leaks(text) == [], output


def test_a_url_set_by_assignment_hides_its_password_too():
    config = ServiceConfig(name="orders")
    config.nats_url = REAL_URL

    for output, text in _outputs_of(config).items():
        assert _leaks(text) == [], output
    assert config.nats_url == REAL_URL


def test_CONTROL_a_config_with_the_default_url_is_unchanged():
    config = ServiceConfig(name="orders")

    assert config.nats_url == ServiceConfig.model_fields["nats_url"].default
    assert urlsplit(ServiceConfig.model_fields["nats_url"].default).hostname in repr(config)
    assert json.loads(config.model_dump_json())["nats_url"] == config.nats_url


def test_CONTROL_a_url_with_nothing_to_hide_is_shown_whole():
    config = _with_url("nats://broker.invalid")

    assert "nats://broker.invalid" in repr(config)
    assert json.loads(config.model_dump_json())["nats_url"] == "nats://broker.invalid"


def test_a_default_url_that_holds_a_password_is_hidden_too():
    """The suite sets the default to the broker it is given, and that URL may carry credentials."""
    field = ServiceConfig.model_fields["nats_url"]
    original = field.default
    try:
        field.default = REAL_URL
        ServiceConfig.model_rebuild(force=True)
        config = ServiceConfig(name="orders")

        for output, text in _outputs_of(config).items():
            assert _leaks(text) == [], output
        assert config.nats_url == REAL_URL
    finally:
        field.default = original
        ServiceConfig.model_rebuild(force=True)
