"""A `nats_url` nats-py cannot connect to and a `health_host` that cannot be bound are refused.

Neither field had a validator. A value of the wrong shape was accepted and failed only when the
connection or the listener was made, and the failure was not always about the value: a wrong
scheme (`http://broker`, `NATS://broker:4222`) or a space made nats-py dial a host that does not
exist and wait out `connect_timeout`, which then reported "no answer within connect_timeout", as if
the broker were down. The checks read the value the way nats-py and asyncio do and refuse what they
cannot use. They check the syntax and never resolve a name, so a host that is merely down passes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cliffracer import ServiceConfig

pytestmark = pytest.mark.unit

USABLE_NATS_URLS = [
    "nats://broker.example:4333",
    "nats://broker:4222",
    "nats://10.0.0.5:14222",
    "nats://[::1]:4222",
    "nats://user:pass@nats.internal:4222",
    "nats://u:p%2Fq@h:4222",  # a '/' in a password, percent-encoded
    "tls://secure.example:4222",
    "ws://gateway.example",
    "wss://gateway.example:443",
    "nats://svc_name.cluster.local:4222",
    "nats://broker",  # no port: nats-py uses 4222
    "broker.example:4333",  # no scheme: nats-py reads it as nats://
    "localhost",
    "broker.example.",
    # What nats-py connects with, so the check must not be stricter than it is.
    " nats://broker:4222",  # urlparse drops a leading space
    "nats://broker:4222\n",  # and a newline, which an env file leaves on the end
    "nats://us er:pw@broker:4222",  # whitespace in the user part
]

# (value, a phrase the reason must contain)
REFUSED_NATS_URLS = [
    ("", "empty"),
    ("   ", "empty"),
    ("nats://", "no host"),
    ("nats://:4222", "no host"),
    ("http://not-nats", "scheme 'http'"),
    ("ftp://h:4222", "scheme 'ftp'"),
    ("NATS://h:4222", "scheme 'NATS'"),
    ("nats://a:4222,nats://b:4222", "second '://'"),
    ("nats://h:notaport", "port"),
    ("nats://h:99999", "port"),
    ("nats://h:0", "port"),
    ("nats://a b:4222", "whitespace"),
    ("nats://broker:4222 ", "port"),  # a trailing space: nats-py reports an invalid URL
    ("nats://a..b:4222", "empty label"),
    ("nats://user/pass@h:4222", "percent-encode"),
]

USABLE_HEALTH_HOSTS = [
    "127.0.0.1",
    "0.0.0.0",
    "::",
    "::1",
    "localhost",
    "my_service",
    "svc.cluster.local",
    "broker.example.",
    "",  # asyncio's spelling of "every interface"
]

REFUSED_HEALTH_HOSTS = [
    ("not a host", "whitespace"),
    ("bad/host", "'/'"),
    ("host:80", "':'"),
    ("http://h", "which a host name cannot"),
    ("a..b", "empty label"),
    ("-x.com", "'-'"),
    ("[::1]", "brackets"),
    ("999.1.1.1", "not one"),
]


@pytest.mark.parametrize("url", USABLE_NATS_URLS)
def test_CONTROL_a_usable_nats_url_is_accepted_unchanged(url):
    assert ServiceConfig(name="a", nats_url=url).nats_url == url


@pytest.mark.parametrize(("url", "reason"), REFUSED_NATS_URLS)
def test_a_nats_url_that_cannot_be_connected_to_is_refused_by_field_and_reason(url, reason):
    with pytest.raises(ValidationError) as refused:
        ServiceConfig(name="a", nats_url=url)

    (error,) = refused.value.errors()
    assert error["loc"] == ("nats_url",)
    assert "nats_url cannot be connected to" in str(refused.value)
    assert reason in str(refused.value), str(refused.value)


@pytest.mark.parametrize("host", USABLE_HEALTH_HOSTS)
def test_CONTROL_a_usable_health_host_is_accepted_unchanged(host):
    assert ServiceConfig(name="a", health_host=host).health_host == host


@pytest.mark.parametrize(("host", "reason"), REFUSED_HEALTH_HOSTS)
def test_a_health_host_that_cannot_be_bound_is_refused_by_field_and_reason(host, reason):
    with pytest.raises(ValidationError) as refused:
        ServiceConfig(name="a", health_host=host)

    (error,) = refused.value.errors()
    assert error["loc"] == ("health_host",)
    assert "health_host cannot be bound" in str(refused.value)
    assert reason in str(refused.value), str(refused.value)


def test_the_defaults_are_usable():
    from cliffracer.core.endpoints import unusable_host, unusable_nats_url

    config = ServiceConfig(name="a")

    assert unusable_nats_url(config.nats_url) is None
    assert unusable_host(config.health_host) is None


@pytest.mark.parametrize(
    ("field", "bad"), [("nats_url", "http://not-nats"), ("health_host", "not a host")]
)
def test_an_assignment_is_refused_the_same_way_and_leaves_the_value_alone(field, bad):
    config = ServiceConfig(name="a")
    before = getattr(config, field)

    with pytest.raises(ValidationError):
        setattr(config, field, bad)

    assert getattr(config, field) == before


def test_a_refused_url_names_the_reason_the_connection_used_to_hide():
    """The old failure for a wrong scheme was a timeout, which named no cause."""
    with pytest.raises(ValidationError, match="scheme 'http' is not one nats-py connects with"):
        ServiceConfig(name="a", nats_url="http://broker:4222")


def test_a_host_that_is_only_unknown_still_passes():
    """The check is syntactic: a name that does not resolve is the connection's to report."""
    from cliffracer.core.endpoints import unusable_host, unusable_nats_url

    assert unusable_host("no-such-host.invalid") is None
    assert unusable_nats_url("nats://no-such-host.invalid:4222") is None


DIFFERENTIAL_CORPUS = [
    *USABLE_NATS_URLS,
    *(url for url, _ in REFUSED_NATS_URLS),
    "nats://h:4222/path",
    "nats://h:4222?x=1",
    "nats://u:p@h:4222",
    "nats://u:p@",
    "nats://h:-1",
    "nats://h:4222\t",
    "\tnats://h:4222",
    "xnats://h:4222",
    "tls://",
    "ws://",
    "wss://:443",
    "h:99999",
    "h:abc",
    ":4222",
    "nats://[::1",
    "nats://::1:4222",
    "nats://none:4222",
    "none",
    "a" * 300,
]


@pytest.mark.parametrize("url", DIFFERENTIAL_CORPUS)
def test_a_url_that_is_accepted_is_one_nats_py_can_parse(url):
    """The check never accepts what nats-py's own parser refuses.

    It reads nats-py's private `_parse_server_uri`, so a release that changes how a URL is read
    reddens this and not a deployed service.
    """
    from nats.aio.client import Client
    from nats.errors import Error

    from cliffracer.core.endpoints import unusable_nats_url

    if unusable_nats_url(url) is not None:
        return
    try:
        Client._parse_server_uri(url)
    except Error as error:
        pytest.fail(f"accepted {url!r}, which nats-py refuses: {error}")
