"""A broker URL is printed without its credentials in every shape it is written in.

`redact_nats_url` withheld the user and password in front of the last "@" and nothing else: a token
or password in the query string (`?token=`, `?pass=`) was printed as it was, a comma-separated
list of servers was cut to its last host, which hid which server failed, and the scheme `nats+tls`
was dropped from the redacted form. Each shape is checked here against the helper, and then where a
URL reaches a printed line: the service's connection error log, the client's `RpcConnectionError`
and the generator's messages.
"""

import itertools
import resource
import subprocess
import sys

import nats.errors
import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.client import RpcConnectionError, ServiceClient
from cliffracer.core.endpoints import BrokerUrl, redact_nats_url
from cliffracer.generate_client.cli import main

pytestmark = pytest.mark.unit

SECRET = "s3cr3t-value"


def _url(template: str, name: str = "") -> str:
    """A URL from a template, so this file holds no formatted broker address of its own."""
    return template.replace("<S>", SECRET).replace("<N>", name)


# This test runs first on purpose: every test below calls the redactor in this process, so a parse
# that never ends would hang the run before a later test could name it.
def _bound_the_child() -> None:
    """A parse that never ends appends to its list on every pass, so it fills memory in a second or
    two: a wall bound alone can arrive after the host has run out. The child is held to 512 MiB of
    address space as well, so either way it ends by itself and the test names the failure."""
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (_url("nats://host:4222/?token=<S>&a=1"), "nats://host:4222/?token=***&a=1"),
        (_url("nats://host:4222/#token=<S>"), "nats://host:4222/#token=***"),
        ("nats://host:4222/?&a=1", "nats://host:4222/?&a=1"),
        ("nats://host:4222/#&a=1", "nats://host:4222/#&a=1"),
    ],
    ids=[
        "query",
        "fragment",
        "query-starting-with-an-ampersand",
        "fragment-starting-with-an-ampersand",
    ],
)
def test_a_url_with_a_query_or_a_fragment_is_returned(url, expected):
    """Every log line passes its broker URL through here, so a parse of its pairs that never ends
    would hang the line and fill memory. The call runs in a separate process under a wall bound and
    a memory bound, so such a parse fails this test by name, and not only through the run's
    per-test timeout or the host's memory. A pair list that begins with a separator is returned
    whole: an end at index 0 is an end, not an absence."""
    code = "import sys; from cliffracer.core.endpoints import redact_nats_url; "
    code += "print(redact_nats_url(sys.argv[1]))"
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, url],
            capture_output=True,
            text=True,
            timeout=5,
            preexec_fn=_bound_the_child,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("redact_nats_url did not return within 5 s for a URL with name=value pairs")
    if result.returncode != 0:
        pytest.fail(
            "redact_nats_url did not return cleanly for a URL with name=value pairs: "
            + result.stderr.strip().splitlines()[-1]
        )
    assert result.stdout == f"{expected}\n"


@pytest.mark.parametrize(
    "url",
    [
        _url("nats://host:4222/?token=<S>"),
        _url("nats://host:4222?pass=<S>"),
        _url("nats://host:4222/?pwd=<S>"),
        _url("nats://host:4222/?password=<S>"),
        _url("nats://host:4222/?user=<S>"),
        _url("nats://host:4222/?API-Key=<S>"),
        _url("nats://host:4222/?to%6Ben=<S>"),
        _url("nats://host:4222/?tls_required=true&auth_token=<S>&x=1"),
        _url("nats://host:4222/?x=1#token=<S>"),
        _url("nats://host:4222#token=<S>"),
        _url("nats://user:pw@host:4222/?token=<S>"),
    ],
)
def test_a_credential_in_the_query_or_the_fragment_is_withheld_and_the_host_is_kept(url):
    out = redact_nats_url(url)

    assert SECRET not in out, out
    assert "host:4222" in out


@pytest.mark.parametrize(
    "name", ["pass", "pwd", "auth", "user", "username", "key", "nkey", "creds", "PASS", "User-Name"]
)
def test_every_name_a_broker_url_carries_a_credential_under_is_withheld(name):
    out = redact_nats_url(_url("nats://host:4222/?<N>=<S>&x=1", name))

    assert SECRET not in out, out
    assert out.endswith("&x=1") and "host:4222" in out


@pytest.mark.parametrize("name", ["bypass", "passenger", "keyboard", "users", "author", "tls"])
def test_CONTROL_a_name_that_merely_contains_one_of_those_is_kept(name):
    url = _url("nats://host:4222/?<N>=visible", name)

    assert redact_nats_url(url) == url


def test_the_parameters_that_are_not_credentials_are_kept():
    out = redact_nats_url(_url("nats://host:4222/?tls_required=true&token=<S>&retries=3"))

    assert out == "nats://host:4222/?tls_required=true&token=***&retries=3"


def test_a_query_with_nothing_to_withhold_is_returned_unchanged():
    url = "nats://host:4222/?tls_required=true&retries=3#frag"

    assert redact_nats_url(url) == url


@pytest.mark.parametrize(
    "url",
    ["a:1, nats://b:2", "nats://h1:4222, nats://h2:4222", "nats://h1:4222,\ttls://h2:4222"],
    ids=["scheme-less-then-scheme", "two-servers", "a-tab"],
)
def test_a_list_with_nothing_to_withhold_is_returned_byte_for_byte(url):
    """Whitespace after a comma included: the list is printed exactly as it was given."""
    assert redact_nats_url(url) == url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("nats://host:4222#password=<S>?y=1", "nats://host:4222#password=***"),
        ("nats://user:pw@host:4222#token=<S>?a", "nats://***@host:4222#token=***"),
        ("nats://host:4222/#pass=<S>&x=1?y", "nats://host:4222/#pass=***&x=1?y"),
    ],
    ids=["plain", "behind-userinfo", "a-later-pair"],
)
def test_a_question_mark_after_the_hash_is_part_of_the_fragment_and_its_credential_is_withheld(
    url, expected
):
    """The fragment starts at the first `#`; a `?` after it does not start a query."""
    assert redact_nats_url(_url(url)) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("nats://host:4222?a=1?password=<S>", "nats://host:4222?a=1?password=***"),
        ("nats://host:4222#a=1#password=<S>", "nats://host:4222#a=1#password=***"),
        ("nats://user:pw@host:4222#a=1#pass=<S>", "nats://***@host:4222#a=1#pass=***"),
        ("nats://host:4222?flag?password=<S>", "nats://host:4222?flag?password=***"),
    ],
    ids=["after-a-second-question-mark", "after-a-second-hash", "behind-userinfo", "after-a-flag"],
)
def test_a_value_that_is_not_a_credential_ends_at_a_question_mark_or_hash(url, expected):
    """`a` names no credential, so its value ends at the next `?` or `#`, and the credential pair
    after it is withheld."""
    assert redact_nats_url(_url(url)) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("nats://host:4222?password=pa?ss=word", "nats://host:4222?password=***"),
        ("nats://host:4222#password=pa#ss", "nats://host:4222#password=***"),
        ("nats://host:4222?token=x?y=1", "nats://host:4222?token=***"),
    ],
    ids=["a-question-mark-in-a-password", "a-hash-in-a-password", "a-question-mark-in-a-token"],
)
def test_CONTROL_a_credentials_value_runs_to_the_next_ampersand_whatever_it_holds(url, expected):
    """Splitting a credential's value at its `?` or `#` would print the rest of it."""
    assert redact_nats_url(url) == expected


def test_a_hash_in_a_query_credential_ends_the_query_and_what_follows_is_the_fragment():
    """A `#` ends a query, so `ss` is the fragment: the consumer reads the password as `pa`."""
    assert redact_nats_url("nats://host:4222?password=pa#ss") == "nats://host:4222?password=***#ss"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "tls://ccm1:49724?x=1#x=QMjybLtj, nlyd.example:07293#pass=<S>",
            "tls://ccm1:49724?x=1#x=QMjybLtj, nlyd.example:07293#pass=***",
        ),
        (
            "ws://tzvu.example:47005?x=QfDMwKUH,[::1]?password=<S>",
            "ws://tzvu.example:47005?x=QfDMwKUH,[::1]?password=***",
        ),
        (
            "nats://10.195.131.60#x=QJXtptUX,10.147.56.238:5962?user=<S>",
            "nats://10.195.131.60#x=QJXtptUX,10.147.56.238:5962?user=***",
        ),
    ],
    ids=["a-second-hash", "a-question-mark-in-a-query", "a-valid-fragment"],
)
def test_a_scheme_less_servers_credential_after_an_earlier_value_is_withheld_and_the_value_kept(
    url, expected
):
    """The earlier pair names no credential, so its value, commas and all, ends at the `?` or `#`
    and is printed; the credential after it is withheld."""
    assert redact_nats_url(_url(url)) == expected


def test_CONTROL_a_query_with_no_credential_name_is_kept_whatever_question_marks_it_holds():
    assert redact_nats_url("nats://host:4222?q=what?x=1") == "nats://host:4222?q=what?x=1"


def test_a_query_before_the_hash_is_still_redacted_and_a_fragment_without_credentials_is_kept():
    assert redact_nats_url("nats://host:4222/?token=abc#frag") == "nats://host:4222/?token=***#frag"


def test_every_server_of_a_list_is_kept_and_each_is_redacted():
    out = redact_nats_url(
        "nats://u:p@a:4222,nats://u2:p2@b:4222,tls://c:4223/?token=" + SECRET + ",wss://d:443"
    )

    assert out == "nats://***@a:4222,nats://***@b:4222,tls://c:4223/?token=***,wss://d:443"


@pytest.mark.parametrize(
    "url",
    [
        "nats://u:<S>,nats://x@h:4222",
        "nats://u:1234<S>,nats://x@h:4222",
        "nats://u:a,nats://<S>@h:4222",
        "nats://u:<S>,tls://x:y@h:4222,nats://z@j:4222",
        "tls://u:<S>,wss://x@h:4222",
        "nats://u:a,nats://x:<S>@h:4222/?token=<S>",
        "nats://u:<S>,nats://b:4222,wss://c:4222,nats://x@h:4222",
    ],
)
def test_a_password_holding_a_comma_and_a_scheme_is_withheld_whole(url):
    """Redaction never prints more of a URL than withholding everything before the last `@` does."""
    out = redact_nats_url(_url(url))

    assert SECRET not in out, out
    assert "u:a" not in out and out.count("@") >= 1


def test_a_server_without_credentials_in_front_of_one_with_them_is_kept():
    assert redact_nats_url("nats://a:4222,nats://u:p@b:4222") == "nats://a:4222,nats://***@b:4222"


@pytest.mark.parametrize("credentialed", itertools.product([False, True], repeat=4))
def test_every_server_of_a_list_is_kept_in_any_order_of_credentialed_and_not(credentialed):
    """Each of the sixteen orders of four servers, with the list and the expectation built apart."""
    given = ",".join(
        _url("nats://user<N>:pw<N>@host<N>:4222" if has else "nats://host<N>:4222", str(n))
        for n, has in enumerate(credentialed)
    )
    expected = ",".join(
        _url("nats://***@host<N>:4222" if has else "nats://host<N>:4222", str(n))
        for n, has in enumerate(credentialed)
    )

    assert redact_nats_url(given) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("nats://h1:4222,nats://u:p@h2:4222", "nats://h1:4222,nats://***@h2:4222"),
        (
            "nats://h1:4222,nats://h2:4222,nats://u:p@h3:4222",
            "nats://h1:4222,nats://h2:4222,nats://***@h3:4222",
        ),
        (
            "nats://h1:4222,nats://u:p@h2:4222,tls://h3:4223",
            "nats://h1:4222,nats://***@h2:4222,tls://h3:4223",
        ),
        ("nats://[::1]:4222,nats://u:p@h2:4222", "nats://[::1]:4222,nats://***@h2:4222"),
        ("nats://h1:4222,nats+tls://u:p@h2:4222", "nats://h1:4222,nats+tls://***@h2:4222"),
        ("nats+tls://h1:4222,nats://u:p@h2:4222", "nats+tls://h1:4222,nats://***@h2:4222"),
        ("tls://h1:4222,nats://u:p@h2:4222", "tls://h1:4222,nats://***@h2:4222"),
        ("ws://h1:80,ws://u:p@h2:80", "ws://h1:80,ws://***@h2:80"),
        ("NATS://H1:4222,NATS://u:p@H2:4222", "NATS://H1:4222,NATS://***@H2:4222"),
        ("NATS://[FE80::1]:4222,NATS://u:p@h2", "NATS://[FE80::1]:4222,NATS://***@h2"),
    ],
)
def test_a_server_in_front_of_a_credentialed_one_is_kept_whatever_its_scheme(url, expected):
    assert redact_nats_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("h1:4222,nats://u:p@h2:4222", "***@h2:4222"),
        ("h1:4222,u:p@h2:4222", "***@h2:4222"),
        ("nats://h1,nats://u:p@h2:4222", "nats://***@h2:4222"),
        ("wss://h1:443/ws,wss://u:p@h2:443", "wss://***@h2:443"),
        ("tls://h1:4223/?x=1,nats://u:p@h2:4222", "tls://***@h2:4222"),
        ("nats://h1:4222#f,nats://u:p@h2:4222", "nats://***@h2:4222"),
    ],
    ids=["scheme-less-first", "scheme-less-list", "no-port", "path", "query", "fragment"],
)
def test_LIMIT_a_piece_that_is_not_exactly_a_scheme_a_host_and_a_port_is_withheld_with_the_next(
    url, expected
):
    """Only exactly a scheme, a host and a numeric port make a piece a server of its own; anything
    else may be, or may run on into, a password, so it is redacted with what follows and only the
    last host is printed."""
    assert redact_nats_url(url) == expected


def test_LIMIT_a_user_and_password_written_as_a_host_and_a_port_are_read_as_a_server():
    """`nats://u:1234,nats://x@h` is one server whose password is `1234,nats://x`, or two servers;
    it reads as two, so the piece in front is printed."""
    assert redact_nats_url("nats://u:1234,nats://x@h:4222") == "nats://u:1234,nats://***@h:4222"


def test_a_credential_in_the_query_of_a_server_in_front_of_a_credentialed_one_is_withheld():
    out = redact_nats_url(_url("tls://h1:4223/?token=<S>,nats://u:p@h2:4222"))

    assert out == "tls://***@h2:4222"


@pytest.mark.parametrize(
    "url",
    [
        "nats://h1:4222?x=1,alice:<S>,nats://x@h2:4222",
        "nats://h1:4222?x=1, nats://h2:4222?pass=<S>,nats://u:p@h3:4222",
        "nats://h1:4222/p, alice:<S>,nats://x@h2:4222",
        "nats://alice:1234/<S>,nats://x@h2:4222",
        "nats://alice:<S>:4222,nats://x@h2:4222",
        "nats://h1:4222/<S>,nats://x@h2:4222",
        "nats://h1:4222#<S>,nats://x@h2:4222",
        "tls://x@h1:80, nats://alice:1234/<S>,nats://x@h2:4222",
        "tls://x@h1:80, nats://u:<S>,nats://x@h2:4222",
        "nats://h0:4222, ws://h1:443/?token=<S>, nats://x@h2:4222",
    ],
    ids=[
        "query-runs-into-a-password",
        "query-hides-a-later-pass",
        "path-runs-into-a-password",
        "digits-then-a-path",
        "password-then-digits",
        "path",
        "fragment",
        "comma-space-then-digits-and-a-path",
        "comma-space-then-a-password",
        "comma-space-then-a-query-token",
    ],
)
def test_a_secret_after_a_front_servers_path_query_or_port_is_withheld(url):
    """Each was printed when a piece with a path, query or fragment counted as a server."""
    out = redact_nats_url(_url(url))

    assert SECRET not in out, out
    assert out.endswith("***@h2:4222") or out.endswith("***@h3:4222"), out


def test_a_broker_url_in_front_of_a_credentialed_one_is_printed_whole_by_its_repr():
    url = BrokerUrl("nats://h1:4222,nats://u:p@h2:4222")

    assert repr(url) == repr("nats://h1:4222,nats://***@h2:4222")


def test_a_comma_in_a_password_is_not_taken_for_a_second_server():
    out = redact_nats_url("nats://svc:p,w@a:4222,nats://svc:p,w@b:4222")

    assert out == "nats://***@a:4222,nats://***@b:4222"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("nats+tls://u:p@host:4222", "nats+tls://***@host:4222"),
        ("NATS+TLS://u:p@host:4222", "NATS+TLS://***@host:4222"),
        ("nats://u:p@host:4222", "nats://***@host:4222"),
        ("wss://u:p@host:443", "wss://***@host:443"),
        ("nats+tls://host:4222", "nats+tls://host:4222"),
    ],
)
def test_the_scheme_is_kept(url, expected):
    assert redact_nats_url(url) == expected


def test_CONTROL_text_in_front_of_a_scheme_separator_that_is_not_a_scheme_is_still_withheld():
    out = redact_nats_url("svctoken://user:pass@host:4222")

    assert "svctoken" not in out and "pass" not in out
    assert out == "***@host:4222"


def test_CONTROL_a_password_holding_a_question_mark_or_a_hash_is_withheld_whole():
    out = redact_nats_url("nats://user:pa?ss#w@host:4222")

    assert out == "nats://***@host:4222"


# -- where a URL reaches a printed line ----------------------------------------------------------

SHAPES = [
    _url("nats://refused.example:4222/?token=<S>"),
    _url("nats://refused.example:4222/?pass=<S>&x=1"),
    _url("nats://user:<S>@refused.example:4222/?token=<S>"),
]


@pytest.mark.parametrize("url", SHAPES)
async def test_the_service_connection_error_log_does_not_carry_the_credential(url, monkeypatch):
    async def refuse(*args, **kwargs):
        raise nats.errors.NoServersError()

    monkeypatch.setattr("cliffracer.core.dial.connect", refuse)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    service = CliffracerService(ServiceConfig(name="redact", nats_url=url, connect_timeout=1.0))
    try:
        with pytest.raises(Exception) as raised:
            await service.connect()
    finally:
        logger.remove(sink)

    text = "\n".join(lines) + repr(raised.value)
    assert "could not reach NATS" in text, text
    assert "refused.example:4222" in text, text
    assert SECRET not in text, text


@pytest.mark.parametrize(
    "url", [*SHAPES, _url("nats://u:<S>@refused.example:4222,nats://u:<S>@other.example:4222")]
)
async def test_the_clients_connection_error_does_not_carry_the_credential(url, monkeypatch):
    async def refuse(*args, **kwargs):
        raise nats.errors.NoServersError()

    monkeypatch.setattr("cliffracer.core.dial.connect", refuse)

    with pytest.raises(RpcConnectionError) as raised:
        await ServiceClient(nats_url=url, service="orders", verify=False)._connection()

    assert SECRET not in str(raised.value), raised.value
    assert "refused.example:4222" in str(raised.value)


def test_the_clients_message_for_a_list_names_every_server_it_tried(monkeypatch):
    import asyncio

    async def refuse(*args, **kwargs):
        raise nats.errors.NoServersError()

    monkeypatch.setattr("cliffracer.core.dial.connect", refuse)
    url = _url("nats://u:<S>@a.example:4222,nats://u:<S>@b.example:4222")

    with pytest.raises(RpcConnectionError) as raised:
        asyncio.run(ServiceClient(nats_url=url, service="orders", verify=False)._connection())

    assert "a.example:4222" in str(raised.value) and "b.example:4222" in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        _url("nats://broker.example:5222/?token=<S>"),
        _url("nats://u:<S>@a.example:5222,nats://u:<S>@b.example:5222"),
        _url("nats+tls://u:<S>@broker.example:5222"),
    ],
)
def test_the_generators_message_does_not_carry_the_credential(monkeypatch, capsys, url):
    async def fetch(*args, **kwargs):
        raise nats.errors.NoServersError()

    monkeypatch.setattr("cliffracer.generate_client.cli.fetch_description", fetch)

    rc = main(["--service", "orders", "--nats-url", url, "--timeout", "1.0"])

    err = capsys.readouterr().err
    assert rc == 3, err
    assert "example:5222" in err, err
    assert SECRET not in err, err


def test_a_comma_and_a_space_separate_servers_as_a_comma_does():
    out = redact_nats_url(_url("nats://h1:4222, nats://u:<S>@h2:4222, tls://h3:4223/?token=<S>"))

    assert out == "nats://h1:4222,nats://***@h2:4222,tls://h3:4223/?token=***"


@pytest.mark.parametrize(
    "url",
    [
        "nats://a@h1:4222,usr:<S>, nats://n@h2:4222",
        "nats://a@h1:4222,usr:<S>,nats://n@h2:4222",
        "nats://a@h1:4222?x=1,usr:<S>, nats://n@h2:4222",
        "nats://a@h1:4222,usr:<S>,\tnats://n@h2:4222",
        "nats://a@h1:4222,usr:<S>,\nnats://n@h2:4222",
        "nats://a@h1:4222,usr:<S>",
        "nats://a@h1:4222,usr:<S>,Sxtra, nats://n@h2:4222",
    ],
    ids=["comma-space", "comma", "query", "tab", "newline", "last", "two-commas"],
)
def test_what_follows_a_comma_after_a_servers_host_is_withheld(url):
    """A host holds no comma, so text after one is the next server's user and password."""
    out = redact_nats_url(_url(url))

    assert SECRET not in out, out
    assert out.startswith("nats://***@h1:4222"), out
    assert ",***" in out, out


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "nats://a@h1:4222,u:<S>,nats://h9:4222,nats://u2@h2:4222",
            "nats://***@h1:4222,***@h2:4222",
        ),
        (
            "nats://a@h1:4222, tok<S>,nats://h9:4222,nats://u2@h2:4222",
            "nats://***@h1:4222,***@h2:4222",
        ),
        ("nats://a@h1:4222,usr:<S>, nats://n@h2:4222", "nats://***@h1:4222,***@h2:4222"),
    ],
    ids=["a-whole-server-in-the-password", "a-token", "the-next-at"],
)
def test_text_after_a_comma_behind_a_host_is_joined_up_to_the_next_at_like_any_password(
    url, expected
):
    """`u:<S>` has no `@` and no numeric port, so it starts a password, and every piece up to the
    next `@` is part of it, a whole server (`nats://h9:4222`) included. The server in front keeps
    its host."""
    assert redact_nats_url(_url(url)) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "nats://a@h1:4222,nats://u:<S>,nats://h9:4222,nats://u2@h2:4222",
            "nats://***@h1:4222,nats://***@h2:4222",
        ),
        ("nats://u:1234,nats://h9:4222,nats://u2@h2", "nats://u:1234,nats://h9:4222,nats://***@h2"),
        ("nats://a@h1:4222,usr:<S>", "nats://***@h1:4222,***"),
    ],
    ids=["the-password-has-its-own-scheme", "a-valid-list", "no-later-at"],
)
def test_CONTROL_a_password_with_its_own_scheme_a_valid_list_and_a_last_run_on_print_as_before(
    url, expected
):
    assert redact_nats_url(_url(url)) == expected


def test_a_token_that_starts_with_a_comma_at_the_start_of_a_list_is_withheld_whole():
    """The list's first piece is empty and opens a join to the next `@`, as any piece with no `@`
    does, so the server written inside the token is not printed."""
    out = redact_nats_url(_url(",nats://h9:4222,NATS://<S>@hjr5:4222"))

    assert out == "***@hjr5:4222"


def test_CONTROL_a_list_that_starts_with_a_comma_and_holds_no_credentials_prints_as_written():
    assert redact_nats_url(",nats://h1:4222") == ",nats://h1:4222"


@pytest.mark.parametrize("separator", [",\t", ",\n", ", \t "], ids=["tab", "newline", "mixed"])
def test_any_whitespace_after_a_comma_separates_servers(separator):
    out = redact_nats_url("nats://h1:4222<SEP>nats://u:p@h2:4222".replace("<SEP>", separator))

    assert out == "nats://h1:4222,nats://***@h2:4222"


def test_a_server_with_no_scheme_and_a_query_is_printed_redacted():
    assert redact_nats_url("h:4222?password=p") == "h:4222?password=***"
