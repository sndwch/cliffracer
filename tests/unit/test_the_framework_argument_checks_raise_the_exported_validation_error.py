"""The `ValidationError` the docs tell a user to catch is the one the argument checks raise.

Three classes carried the name: the exported `cliffracer.ValidationError` (a `ServiceError`),
the argument checks' own class (a `ValueError`), and pydantic's. A handler wrapping a call in
`except cliffracer.ValidationError` could never catch a framework argument check, which raised
the second. The argument checks' class now derives from the exported one and stays a
`ValueError`, so both catches work; pydantic's is a separate class and the docs say so.
"""

import pickle

import pytest
from pydantic import ValidationError as PydanticValidationError

import cliffracer
from cliffracer import ServiceConfig
from cliffracer.core.exceptions import CliffracerError, ServiceError
from cliffracer.core.validation import ValidationError, validate_timeout

pytestmark = pytest.mark.unit


def _check_that_fails() -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        validate_timeout("nope")  # type: ignore[arg-type]
    return caught.value


def test_an_argument_check_is_caught_by_the_exported_class():
    with pytest.raises(cliffracer.ValidationError):
        validate_timeout("nope")  # type: ignore[arg-type]


def test_it_is_still_a_value_error_so_an_existing_catch_keeps_working():
    with pytest.raises(ValueError):
        validate_timeout("nope")  # type: ignore[arg-type]


def test_it_sits_in_the_cliffracer_hierarchy():
    error = _check_that_fails()

    assert isinstance(error, ServiceError) and isinstance(error, CliffracerError)
    assert str(error)


def test_it_survives_pickle_like_every_other_cliffracer_error():
    error = _check_that_fails()

    restored = pickle.loads(pickle.dumps(error))

    assert type(restored) is ValidationError
    assert str(restored) == str(error)


def test_CONTROL_a_service_config_failure_is_pydantics_class_not_the_exported_one():
    """The docs say so; this keeps the sentence true."""
    with pytest.raises(PydanticValidationError) as caught:
        ServiceConfig(**{"name": "x", "a_key_it_does_not_have": 1})

    assert not isinstance(caught.value, cliffracer.ValidationError)
