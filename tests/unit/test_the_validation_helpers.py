"""The batch-size, string-length, password and username helpers, at their edges.

`validate_timeout` has its own file. These four had no test anywhere, which is
how `validate_batch_size(True)` came to return `True`: `isinstance(True, int)`
is True, so a flag passed where a count belongs was accepted as a batch of one.
It is refused now, as `validate_timeout` already refuses it.

Each bound is asserted from both sides: the last value accepted and the first
refused, read from the constants the helpers use rather than restated.
"""

import pytest

from cliffracer.core.validation import (
    NumericBounds,
    StringLimits,
    ValidationError,
    validate_batch_size,
    validate_password,
    validate_string_length,
    validate_username,
)

pytestmark = pytest.mark.unit


class TestBatchSize:
    def test_the_bounds_are_accepted(self):
        assert validate_batch_size(NumericBounds.MIN_BATCH_SIZE) == NumericBounds.MIN_BATCH_SIZE
        assert validate_batch_size(NumericBounds.MAX_BATCH_SIZE) == NumericBounds.MAX_BATCH_SIZE

    @pytest.mark.parametrize(
        "size", [NumericBounds.MIN_BATCH_SIZE - 1, NumericBounds.MAX_BATCH_SIZE + 1]
    )
    def test_one_past_either_bound_is_refused(self, size):
        with pytest.raises(ValidationError, match=f"got {size}"):
            validate_batch_size(size)

    @pytest.mark.parametrize("size", [True, False])
    def test_a_bool_is_not_a_count(self, size):
        with pytest.raises(ValidationError, match="got bool"):
            validate_batch_size(size)

    @pytest.mark.parametrize("size", [2.0, "8", None])
    def test_a_value_that_is_not_an_integer_is_refused_by_type(self, size):
        with pytest.raises(ValidationError, match="must be an integer"):
            validate_batch_size(size)


class TestStringLength:
    def test_the_bounds_are_accepted_and_the_value_is_returned_unchanged(self):
        assert validate_string_length("abc", min_length=3, max_length=5) == "abc"
        assert validate_string_length("abcde", min_length=3, max_length=5) == "abcde"

    def test_one_under_the_minimum_is_refused(self):
        with pytest.raises(ValidationError, match="at least 3 characters, got 2"):
            validate_string_length("ab", min_length=3, max_length=5)

    def test_one_over_the_maximum_is_refused(self):
        with pytest.raises(ValidationError, match="at most 5 characters, got 6"):
            validate_string_length("abcdef", min_length=3, max_length=5)

    def test_surrounding_whitespace_counts_and_is_not_trimmed(self):
        assert validate_string_length(" ab ", min_length=4, max_length=4) == " ab "
        with pytest.raises(ValidationError, match="got 5"):
            validate_string_length(" abc ", max_length=4)

    def test_the_field_name_is_in_the_message(self):
        with pytest.raises(ValidationError, match="^Email must be at least"):
            validate_string_length("a", min_length=3, field_name="Email")

    def test_no_bounds_accepts_any_length(self):
        assert validate_string_length("") == ""

    def test_a_value_that_is_not_a_string_is_refused(self):
        with pytest.raises(ValidationError, match="must be a string, got int"):
            validate_string_length(12345, min_length=1)


class TestPassword:
    def test_the_minimum_length_is_accepted_and_one_under_is_refused(self):
        at_minimum = "p" * StringLimits.MIN_PASSWORD_LENGTH
        assert validate_password(at_minimum) == at_minimum
        with pytest.raises(ValidationError, match="Password must be at least"):
            validate_password(at_minimum[:-1])

    def test_the_maximum_length_is_accepted_and_one_over_is_refused(self):
        at_maximum = "p" * StringLimits.MAX_PASSWORD_LENGTH
        assert validate_password(at_maximum) == at_maximum
        with pytest.raises(ValidationError, match="Password must be at most"):
            validate_password(at_maximum + "p")


class TestUsername:
    @pytest.mark.parametrize("name", ["alice", "alice_1", "alice-1", "alice.smith"])
    def test_letters_digits_underscores_hyphens_and_dots_are_accepted(self, name):
        assert validate_username(name) == name

    @pytest.mark.parametrize("name", ["alice smith", "alice@example", "alice/1", "al!ce"])
    def test_any_other_character_is_refused(self, name):
        with pytest.raises(ValidationError, match="can only contain"):
            validate_username(name)

    def test_the_length_bounds_come_from_the_string_limits(self):
        assert validate_username("a" * StringLimits.MIN_USERNAME_LENGTH)
        assert validate_username("a" * StringLimits.MAX_USERNAME_LENGTH)
        with pytest.raises(ValidationError, match="Username must be at least"):
            validate_username("a" * (StringLimits.MIN_USERNAME_LENGTH - 1))
        with pytest.raises(ValidationError, match="Username must be at most"):
            validate_username("a" * (StringLimits.MAX_USERNAME_LENGTH + 1))
