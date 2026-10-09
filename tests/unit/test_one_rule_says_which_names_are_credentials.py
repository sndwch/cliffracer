"""The log redactor and the dead-letter publisher ask one predicate which names are credentials.

They used to keep two lists that disagreed: the redactor did not know `cookie`, `set-cookie`, `jwt`,
`bearer`, `session*`, `secret_key`, `signing_key` or `passphrase`, which the publisher withheld in part, and
neither knew the header a configured `AuthExtension(header=...)` reads except the publisher. The
tables below are the two old lists written out as literals, so a name either of them matched has to
stay matched: nothing is redacted or withheld less than before.
"""

import pytest

from cliffracer.core.credentials import (
    CREDENTIAL_FRAGMENTS,
    canonical_name,
    credential_names_of,
    is_credential_name,
)

pytestmark = pytest.mark.unit

# The redactor's old rule: the canonical key equals one of these or ends with `_<token>`.
OLD_LOG_TOKENS = {
    "access_key", "access_token", "apikey", "api_key", "authorization", "client_secret",
    "credential", "credentials", "passwd", "password", "private_key", "secret", "token",
}  # fmt: skip
# The publisher's old rule: the lower-cased header name equals one of the first two, or contains one
# of the rest.
OLD_DLQ_NAMES = {"cookie", "set-cookie"}
OLD_DLQ_FRAGMENTS = [
    "authorization", "token", "secret", "password", "passwd", "credential", "api-key", "api_key",
    "apikey", "jwt", "bearer", "session",
]  # fmt: skip

NAMES_THE_ISSUE_LISTS = [
    "secret_key", "jwt", "cookie", "Set-Cookie", "session_id", "sessionid", "bearer",
    "authorization_header", "signing_key", "encryption_key", "passphrase",
]  # fmt: skip


def _spellings(token: str) -> list[str]:
    return [
        token,
        token.upper(),
        token.replace("_", "-"),
        f"x_{token}",
        f"X-{token.replace('_', '-')}",
    ]


@pytest.mark.parametrize("token", sorted(OLD_LOG_TOKENS))
def test_every_name_the_log_redactor_matched_is_still_matched(token):
    for spelling in _spellings(token):
        assert is_credential_name(spelling), spelling


@pytest.mark.parametrize("name", sorted(OLD_DLQ_NAMES))
def test_every_name_the_publisher_withheld_exactly_is_still_withheld(name):
    assert is_credential_name(name)
    assert is_credential_name(name.upper())


@pytest.mark.parametrize("fragment", OLD_DLQ_FRAGMENTS)
def test_every_fragment_the_publisher_looked_for_is_still_looked_for(fragment):
    for name in (fragment, f"x-{fragment}-y", f"My{fragment.title().replace('-', '')}"):
        assert is_credential_name(name), name


@pytest.mark.parametrize("name", NAMES_THE_ISSUE_LISTS)
def test_the_names_one_list_missed_are_matched_now(name):
    assert is_credential_name(name)


def test_the_header_an_extension_reads_a_credential_from_is_a_credential():
    assert not is_credential_name("x-svc-id")
    assert is_credential_name("x-svc-id", ["x-svc-id"])
    assert is_credential_name("X_Svc_Id", ["x-svc-id"])


@pytest.mark.parametrize(
    "name", ["x-request-id", "trace_id", "user", "content-type", "message", ""]
)
def test_CONTROL_an_ordinary_name_is_not_a_credential(name):
    assert not is_credential_name(name)


def test_the_fragments_are_written_in_the_form_the_rule_compares_in():
    assert all(fragment == canonical_name(fragment) for fragment in CREDENTIAL_FRAGMENTS)


def test_credential_names_of_reads_a_string_header_from_each_extension():
    class WithHeader:
        header = "X-Token-Here"

    class NoHeader:
        pass

    class NotAString:
        header = 5

    assert credential_names_of([WithHeader(), NoHeader(), NotAString()]) == {"x-token-here"}
    assert credential_names_of(None) == frozenset()


@pytest.mark.parametrize("name", ["-Cookie-", "__cookie__", "Set-Cookie:"])
def test_a_whole_name_credential_wrapped_in_punctuation_is_one(name):
    """`cookie` and `set_cookie` are matched as whole names, after case and punctuation are
    dropped, so punctuation around them does not hide them."""
    assert is_credential_name(name)
