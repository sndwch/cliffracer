"""A password made of whitespace alone is refused, and what else is accepted is stated.

`validate_password` checked only the length, so eight spaces were a password and `create_user`
made a user who could log in with them. A password whose every character is whitespace
(`str.isspace`) is now refused, after the type and length checks, with a message that names the
rule. The password is still returned unchanged, so surrounding whitespace is kept, and no
composition rule is added: NIST SP 800-63B advises against them.

What stays accepted is pinned as much as what is refused, because it is a decision: a password of
zero-width spaces (U+200B) is invisible but is not whitespace to `str.isspace`, so it passes.
Refusing it would be a rule about invisible characters, which is a composition rule.
"""

import pytest

from cliffracer.core.validation import StringLimits, ValidationError, validate_password

pytestmark = pytest.mark.unit

MINIMUM = StringLimits.MIN_PASSWORD_LENGTH
MAXIMUM = StringLimits.MAX_PASSWORD_LENGTH

# Each is `MINIMUM` or more characters, so the length check passes and only the rule can refuse.
ALL_WHITESPACE = [
    pytest.param(" " * MINIMUM, id="spaces"),
    pytest.param("\t" * MINIMUM, id="tabs"),
    pytest.param("\n" * MINIMUM, id="newlines"),
    pytest.param(" \t\n\r\x0b\x0c  ", id="a mix of ASCII whitespace"),
    pytest.param("\xa0" * MINIMUM, id="no-break spaces U+00A0"),
    pytest.param("　" * MINIMUM, id="ideographic spaces U+3000"),
    pytest.param(" " * MINIMUM, id="em spaces U+2003"),
    pytest.param(" " * MAXIMUM, id="spaces at the maximum length"),
]


@pytest.mark.parametrize("password", ALL_WHITESPACE)
def test_a_password_of_whitespace_alone_is_refused_and_the_message_names_the_rule(password):
    assert len(password) >= MINIMUM and password.isspace()

    with pytest.raises(ValidationError, match="must contain at least one character that is not"):
        validate_password(password)


@pytest.mark.parametrize(
    "password",
    [
        pytest.param(" " * (MINIMUM - 1) + "a", id="one character among spaces"),
        pytest.param(" a      ", id="surrounded by whitespace"),
        pytest.param("a" + " " * (MINIMUM - 1), id="trailing whitespace"),
        pytest.param("pass word with spaces", id="spaces inside"),
        pytest.param("a" * MINIMUM, id="the same character repeated"),
    ],
)
def test_CONTROL_a_password_with_a_character_that_is_not_whitespace_is_accepted_unchanged(password):
    """No composition rule: only a password with nothing but whitespace is refused."""
    assert validate_password(password) == password


def test_a_password_of_zero_width_spaces_still_passes_and_that_is_stated():
    """U+200B is invisible, and `str.isspace` says it is not whitespace. Closing this would be a
    rule about invisible characters, which is the composition rule NIST SP 800-63B advises
    against, so it stays out of this one and the docstring says what is and is not checked."""
    password = "​" * MINIMUM

    assert not password.isspace()
    assert validate_password(password) == password
    assert "zero-width" in (validate_password.__doc__ or "")


def test_the_length_checks_come_first_so_a_short_run_of_spaces_is_a_length_error():
    with pytest.raises(ValidationError, match="Password must be at least"):
        validate_password(" " * (MINIMUM - 1))
    with pytest.raises(ValidationError, match="Password must be at most"):
        validate_password(" " * (MAXIMUM + 1))


def test_a_value_that_is_not_a_string_is_still_refused_as_one():
    with pytest.raises(ValidationError, match="must be a string"):
        validate_password(12345678)  # type: ignore[arg-type]


def test_the_docstring_says_what_is_checked():
    doc = validate_password.__doc__ or ""

    assert "whitespace" in doc and "composition" in doc and "MIN_PASSWORD_LENGTH" in doc
