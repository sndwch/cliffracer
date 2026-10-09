"""Property: a server list printed for a reader withholds every credential planted in it.

Server lists are generated from a seed with users, passwords, tokens and credential query values
planted in them (`tests.fixtures.properties.urls`), and each is printed the two ways a reader sees
one: `redact_nats_url`, which every log line and error message uses, and the `cliffracer-dlq`
command's host column, `_host`. No fragment of a planted secret may appear in either, except where
one of the stated limits covers it:

- U-L1 / U-L2: a user and a password, or a token, written as a host and a numeric port
  (`nats://user:1234`), which cannot be told from one, so `redact_nats_url` prints that server whole
  and `_host` prints it as its host and port (its host alone when the port is 0);
- U-L3: a password holding an unescaped "@" later followed by a comma and a scheme, which has the
  shape of two servers, so the text between that "@" and that comma is printed as a host;
- U-L4: a password holding a whole server between commas, after text that ends a server: an
  unescaped "@" (`nats://u:pw@h1,nats://h9:4222,nats://u2@h2`) or a password of digits
  (`nats://u:1234,nats://h9:4222,nats://u2@h2`). Each of those is also a valid list of three
  servers, so that server is printed as one.

Each printed server that shows a fragment is one finding, so a list showing two is judged by two
limits.

The CONTROL prints each list as it is given and must find planted secrets.
"""

import random
import re

import cliffracer_dlq.cli as dlq_cli
import pytest

from cliffracer.core import endpoints
from tests.fixtures.properties import (
    Finding,
    Limit,
    assert_control_finds,
    assert_matches_only,
    assert_only_known_limits,
    cases,
    seeds,
)
from tests.fixtures.properties.urls import generate, leaked

pytestmark = pytest.mark.unit

SEED = 7
CASES = 4000
CONTROL_CASES = 300
#: A third of what the CONTROL found when measured on seed 7: 1173 printed servers showing a planted
#: fragment outside every limit, in 300 lists.
CONTROL_FLOOR = 391

_SCHEME = r"(?:nats\+tls|nats|tls|ws|wss)://"
_SERVERS = re.compile(rf",(?={_SCHEME})", re.IGNORECASE)
_USER_AND_DIGITS = re.compile(rf"{_SCHEME}(?:\[[0-9a-f:.]+\]|[a-z0-9._-]+):\d+", re.IGNORECASE)
_DIGITS_OR_USER_AND_DIGITS = re.compile(r"(?:[^:,@/?#]+:)?\d+")
_HOST_AND_DIGITS = re.compile(r"(?:[A-Za-z+]+://)?([^/?#:,]+):\d+")
_UNESCAPED_AT_THEN_SERVER = re.compile(rf"@([^@]*?),{_SCHEME}", re.IGNORECASE)


def _printed(url: str) -> dict[str, str]:
    return {"redact_nats_url": endpoints.redact_nats_url(url), "_host": dlq_cli._host(url)}


def findings(count: int = CASES) -> list[Finding]:
    found: list[Finding] = []
    for seed in seeds(SEED):
        rng = random.Random(seed)
        for index in range(cases(count)):
            url, secrets = generate(rng)
            found += _findings_of(seed, index, url, secrets)
    return found


def _findings_of(seed: int, index: int, url: str, secrets: list[str]) -> list[Finding]:
    """A finding for each server, of each way of printing `url`, that shows a fragment of a planted
    secret."""
    found: list[Finding] = []
    for function, text in _printed(url).items():
        for server in text.split(","):
            fragments = leaked(server, secrets, url)
            if fragments:
                found.append(
                    Finding(
                        seed,
                        index,
                        f"{function} printed {sorted(fragments)} in {server!r} of {text!r}",
                        f"url = {url!r}\nsecrets = {secrets!r}",
                        {
                            "function": function,
                            "server": server.strip(),
                            "leaked": fragments,
                            "secrets": secrets,
                        },  # fmt: skip
                    )
                )
    return found


def _a_planted_user_and_digits(server: str, secrets: list[str]) -> bool:
    """Whether `server` is a host and a numeric port that is planted text: a user and a password
    (`user:1234`), or a token that starts that way (`4:30723,...`), written as a host and a port,
    not a server that is one."""
    match = _HOST_AND_DIGITS.fullmatch(server)
    if not match:
        return False
    address = server.split("://")[-1].lower()
    return match.group(1).lower() in {s.lower() for s in secrets} or any(
        s.split(",")[0].lower() == address for s in secrets
    )


def _redacted_as_user_and_digits(finding: Finding) -> bool:
    detail = finding.detail
    return (
        detail["function"] == "redact_nats_url"
        and bool(_USER_AND_DIGITS.fullmatch(detail["server"]))
        and _a_planted_user_and_digits(detail["server"], detail["secrets"])
    )


def _host_of_user_and_digits(finding: Finding) -> bool:
    """`_host` prints the host and the port, and the host alone when the port is 0."""
    detail = finding.detail
    if detail["function"] != "_host":
        return False
    server, secrets = detail["server"], detail["secrets"]
    return _a_planted_user_and_digits(server, secrets) or (
        ":" not in server and _a_planted_user_and_digits(f"{server}:0", secrets)
    )


def _within(finding: Finding, parts: list[str]) -> bool:
    return bool(parts) and all(any(f in part for part in parts) for f in finding.detail["leaked"])


def _unescaped_at_before_a_server(finding: Finding) -> bool:
    between: list[str] = []
    for secret in finding.detail["secrets"]:
        between += [m.group(1) for m in _UNESCAPED_AT_THEN_SERVER.finditer(secret)]
    return _within(finding, between)


def _a_server_inside_a_password_after_an_ended_server(finding: Finding) -> bool:
    """The printed server is written whole inside a password (`pw,nats://h9:4222,nats://u2`), after
    text in that password that ended a server: an unescaped "@" anywhere before it (`pw@h1,`), or
    a password, or a token, that starts as digits or a user and digits (`u:1234,`, `82:7176546,`).
    The list then reads as valid servers, that one among them."""
    detail = finding.detail
    if not _HOST_AND_DIGITS.fullmatch(detail["server"]) or _a_planted_user_and_digits(
        detail["server"], detail["secrets"]
    ):
        return False
    whole = []
    for secret in detail["secrets"]:
        first, *rest = secret.split(",")
        ended = "@" in first or bool(_DIGITS_OR_USER_AND_DIGITS.fullmatch(first))
        for part in rest:
            if ended and _USER_AND_DIGITS.fullmatch(part):
                whole.append(part)
            ended = ended or "@" in part
    return _within(finding, whole)


LIMITS = [
    Limit(
        "U-L1 redact: user and digits read as a host and a port",
        "`scheme://user:1234` cannot be told from a host and a port, so it is printed as a server",
        _redacted_as_user_and_digits,
    ),
    Limit(
        "U-L2 _host: user and digits read as a host and a port",
        "the same reading, in the host column of the dead-letter command",
        _host_of_user_and_digits,
    ),
    Limit(
        "U-L3 an unescaped @ before a comma and a scheme",
        "the password has the shape of two servers; its text after the @ is printed as a host",
        _unescaped_at_before_a_server,
    ),
    Limit(
        "U-L4 a whole server inside a password, after an unescaped @ or digits",
        "`pw@h1,nats://h9:4222,` and `u:1234,nats://h9:4222,` read as valid lists, it among them",
        _a_server_inside_a_password_after_an_ended_server,
    ),
]


def test_a_printed_server_list_withholds_every_planted_credential():
    assert_only_known_limits(findings(), LIMITS, check="U (URL redaction)")


#: A finding from seed 7 for each limit, which that limit covers and no other: the limit, the
#: printer, the list, its secrets and the server printed.
PINNED = [
    (
        "U-L1 redact: user and digits read as a host and a port",
        "redact_nats_url",
        "nats://UZtyvea:65899,nats://rmpQfqfTkV@fvw8",
        ["UZtyvea", "65899,nats://rmpQfqfTkV"],
        "nats://UZtyvea:65899",
    ),
    (
        "U-L2 _host: user and digits read as a host and a port",
        "_host",
        "nats://UZtyvea:65899,nats://rmpQfqfTkV@fvw8",
        ["UZtyvea", "65899,nats://rmpQfqfTkV"],
        "nats://UZtyvea:65899",
    ),
    (
        "U-L3 an unescaped @ before a comma and a scheme",
        "redact_nats_url",
        "UsSYEbC:8@88427,nats://h9:4222821@ezq5:44803",
        ["UsSYEbC", "8@88427,nats://h9:4222821"],
        "***@88427",
    ),
    (
        "U-L4 a whole server inside a password, after an unescaped @ or digits",
        "redact_nats_url",
        "UgemLJG:SdU%@40aWQxk,nats://h9:4222,nats://TgQe@bbd4,nats://SsrcmQnx@rtj2:25546",
        ["UgemLJG", "SdU%@40aWQxk,nats://h9:4222,nats://TgQe", "SsrcmQnx"],
        "nats://h9:4222",
    ),
]


@pytest.mark.parametrize(
    ("name", "function", "url", "secrets", "server"), PINNED, ids=["U-L1", "U-L2", "U-L3", "U-L4"]
)
def test_each_limit_covers_its_pinned_example_and_no_other_limit_does(
    name, function, url, secrets, server
):
    (finding,) = [
        f
        for f in _findings_of(7, 0, url, secrets)
        if f.detail["function"] == function and f.detail["server"] == server
    ]

    assert_matches_only(finding, name, LIMITS)


#: U-L1's stated example. Read as one server, its user is `user` and its password `4222,nats://u2`.
U_L1_STATED = "nats://user:4222,nats://u2@h2"


def test_the_user_and_digits_stated_example_is_judged_by_that_limit_alone():
    """Each printer shows planted text in one server, and each of those two findings is covered by
    that printer's user-and-digits limit and by no other."""
    found = _findings_of(SEED, 0, U_L1_STATED, ["user", "4222,nats://u2"])
    (redacted,) = [f for f in found if f.detail["function"] == "redact_nats_url"]
    (host,) = [f for f in found if f.detail["function"] == "_host"]

    assert len(found) == 2
    assert_matches_only(redacted, "U-L1 redact: user and digits read as a host and a port", LIMITS)
    assert_matches_only(host, "U-L2 _host: user and digits read as a host and a port", LIMITS)


@pytest.mark.parametrize(
    ("url", "printed"),
    [
        ("ws://UApbJBp:SP@CdgA,nats://UQ@[::1]", "ws://***@CdgA,nats://***@[::1]"),
        (U_L1_STATED, "nats://user:4222,nats://***@h2"),
        (
            "nats://u:pw@h1,nats://h9:4222,nats://u2@h2",
            "nats://***@h1,nats://h9:4222,nats://***@h2",
        ),
        (
            "nats://u:1234,nats://h9:4222,nats://u2@h2",
            "nats://u:1234,nats://h9:4222,nats://***@h2",
        ),
    ],
    ids=[
        "U-L3-unescaped-at",
        "U-L1-user-and-digits",
        "U-L4-after-an-unescaped-at",
        "U-L4-after-digits",
    ],
)
def test_a_stated_limit_prints_what_it_says(url, printed):
    """Each URL is also a valid list of servers (`nats://u:1234` is a host and a port,
    `nats://u:pw@h1` a server), and this is what that valid list prints: the same bytes holding a
    password print the same."""
    assert endpoints.redact_nats_url(url) == printed


@pytest.mark.parametrize(
    "url",
    [
        "ws://UApbJBp:SP%40CdgA,nats://UQ@[::1]",
        "nats://user:p@ss@host:4222",
        "nats://u:a@b,c@host:4222",
        "nats://u:Spw,nats://h7:6543,nats://u2@h2",
    ],
    ids=[
        "escaped-at",
        "unescaped-at-in-one-server",
        "unescaped-at-before-a-comma-with-no-scheme",
        "a-server-inside-a-password-its-own-server-starts",
    ],
)
def test_outside_the_stated_limit_the_password_is_withheld_whole(url):
    printed = endpoints.redact_nats_url(url)

    assert not [piece for piece in ("CdgA", "ss@", "b,c", "h7:6543") if piece in printed], printed


def test_a_scheme_less_user_and_password_before_a_later_server_are_withheld_whole():
    """A run-on after a credentialed server's host (`,Uabcdef:Spasswd`) is the next server's user
    and password, written without a scheme; it is split off the host only when an "@" follows, so
    here it stays with what follows and the password is withheld by both printers."""
    url = "nats://a@h1,Uabcdef:Spasswd,nats://h2:4222"

    for printed in (endpoints.redact_nats_url(url), dlq_cli._host(url)):
        assert "Spasswd" not in printed, printed


def test_CONTROL_a_list_printed_as_given_shows_the_planted_secrets(monkeypatch):
    printed: list[str] = []

    def as_given(url: str) -> str:
        printed.append(url)
        return url

    monkeypatch.setattr(endpoints, "redact_nats_url", as_given)
    monkeypatch.setattr(dlq_cli, "_host", as_given)

    shown = [f for f in findings(CONTROL_CASES) if not any(limit.covers(f) for limit in LIMITS)]

    lists = cases(CONTROL_CASES) * len(seeds(SEED))
    assert len(printed) == 2 * lists, (
        "the printers replaced are not the ones each list goes through"
    )
    assert_control_finds(shown, at_least=CONTROL_FLOOR, control="the list printed as given")
