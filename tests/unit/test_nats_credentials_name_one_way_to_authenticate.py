"""`nats_user`/`nats_password`, `nats_token` and `nats_credentials_file` name one way in.

`nats_auth_kwargs()` forwards whatever is set, and nats-py resolves two methods by its own precedence,
so which credential was used was decided by the client library and not by the configuration the
operator wrote. A user with no password sent half of a credential. Both are refused where the config
is built, naming what was set.
"""

import pytest

from cliffracer import ServiceConfig

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("given", "named"),
    [
        (
            {"nats_user": "u", "nats_password": "p", "nats_token": "t"},
            ["nats_user + nats_password", "nats_token"],
        ),
        (
            {"nats_user": "u", "nats_password": "p", "nats_credentials_file": "/c.creds"},
            ["nats_user + nats_password", "nats_credentials_file"],
        ),
        (
            {"nats_token": "t", "nats_credentials_file": "/c.creds"},
            ["nats_token", "nats_credentials_file"],
        ),
        (
            {
                "nats_user": "u",
                "nats_password": "p",
                "nats_token": "t",
                "nats_credentials_file": "/c",
            },
            ["nats_user + nats_password", "nats_token", "nats_credentials_file"],
        ),
    ],
    ids=["user and token", "user and file", "token and file", "all three"],
)
def test_two_ways_to_authenticate_are_refused_and_named(given, named):
    with pytest.raises(ValueError) as caught:
        ServiceConfig(name="svc", **given)

    message = str(caught.value)
    assert "more than one way to authenticate" in message, message
    for name in named:
        assert name in message, (name, message)


@pytest.mark.parametrize(
    ("given", "missing"),
    [({"nats_user": "u"}, "nats_password"), ({"nats_password": "p"}, "nats_user")],
    ids=["user without password", "password without user"],
)
def test_half_a_credential_is_refused_naming_what_is_missing(given, missing):
    with pytest.raises(ValueError, match=f"{missing} is not set"):
        ServiceConfig(name="svc", **given)


@pytest.mark.parametrize(
    ("given", "kwargs"),
    [
        ({}, {}),
        ({"nats_user": "u", "nats_password": "p"}, {"user": "u", "password": "p"}),
        ({"nats_token": "t"}, {"token": "t"}),
        ({"nats_credentials_file": "/c.creds"}, {"user_credentials": "/c.creds"}),
    ],
    ids=["none", "user and password", "token", "credentials file"],
)
def test_each_single_way_is_accepted_and_forwarded_as_it_was(given, kwargs):
    assert ServiceConfig(name="svc", **given).nats_auth_kwargs() == kwargs
