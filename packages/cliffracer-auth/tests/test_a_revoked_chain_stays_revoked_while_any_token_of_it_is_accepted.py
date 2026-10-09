"""A revoked chain stays revoked for as long as any token of it can be accepted.

A token whose lifetime (`exp` minus `iat`) is longer than this service's `token_expiry_hours` is
refused, and so is one with no `iat`. `revoke_token` holds a chain's revocation for that longest
lifetime (plus the leeway on each side) past the later of now and the chain's refresh cap, since a
host sharing the key does not see the revocation and may go on refreshing the chain until its
`oiat` plus `refresh_max_lifetime_hours`. With no cap the chain is kept for the life of the process.
So no token of a revoked chain is accepted again, whichever host minted or refreshed it.

One fake clock stands in for all three readers: `simple_auth.time` (revocations), `simple_auth`'s
`datetime` (minting and the refresh cap) and PyJWT's (`exp` and `iat`). Each test that moves it also
shows a token that is not revoked still accepted at that moment, so the clock is known to have
reached PyJWT.
"""

import types
from datetime import datetime

import jwt
import jwt.api_jwt
import pytest
from cliffracer_auth import simple_auth
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

pytestmark = pytest.mark.unit

SECRET = "x" * 40
HOUR = 3600.0


class Clock:
    """The time every reader sees, in epoch seconds."""

    def __init__(self, monkeypatch, at: float) -> None:
        self.at = at
        clock = self

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(clock.at, tz)

        monkeypatch.setattr(simple_auth, "time", types.SimpleNamespace(time=lambda: clock.at))
        monkeypatch.setattr(simple_auth, "datetime", Frozen)
        monkeypatch.setattr(jwt.api_jwt, "datetime", Frozen)


START = 1_800_000_000.0


def _service(**config) -> SimpleAuthService:
    svc = SimpleAuthService(
        AuthConfig(secret_key=SECRET, pbkdf2_iterations=1000, token_expiry_hours=1, **config)
    )
    svc.create_user("alice", "alice@example.com", "s3cret-password")
    return svc


def _peer_token(*, jti: str, iat: float | None, lives: float, cid: str | None = None) -> str:
    """A token a host sharing the key mints, as this service would but with its own lifetime."""
    claims = {
        "jti": jti,
        "cid": cid if cid is not None else jti,
        "user_id": "user_1",
        "username": "alice",
        "email": "alice@example.com",
        "exp": (iat if iat is not None else START) + lives,
        "oiat": iat if iat is not None else START,
    }
    if iat is not None:
        claims["iat"] = iat
    return jwt.encode(claims, SECRET, algorithm="HS256")


def test_a_peer_token_living_longer_than_this_service_allows_is_refused(monkeypatch):
    Clock(monkeypatch, START)

    assert _service().validate_token(_peer_token(jti="L", iat=START, lives=48 * HOUR)) is None


def test_CONTROL_a_peer_token_with_this_services_lifetime_is_accepted(monkeypatch):
    Clock(monkeypatch, START)

    assert _service().validate_token(_peer_token(jti="L", iat=START, lives=HOUR)) is not None


def test_a_token_with_no_iat_is_refused(monkeypatch):
    Clock(monkeypatch, START)

    assert _service().validate_token(_peer_token(jti="L", iat=None, lives=HOUR)) is None


def test_a_long_lived_ancestor_of_a_revoked_chain_is_refused_after_the_revocation_is_swept(
    monkeypatch,
):
    """The reported case: a 48 h login from a key-sharing host, a refresh here, a revocation of the
    refreshed token, and two hours later another revocation sweeps the chain's entry."""
    clock = Clock(monkeypatch, START)
    svc = _service()
    login = _peer_token(jti="L", iat=START, lives=48 * HOUR)
    # A service that accepts the login refreshes it, and the refreshed token is revoked; this one
    # refuses the login, so there is nothing to refresh.
    child = svc.refresh_token(login)
    if child is not None:
        assert svc.revoke_token(child) is True

    clock.at = START + 2 * HOUR
    svc.revoke_token(_peer_token(jti="other", iat=clock.at, lives=HOUR))

    assert svc.validate_token(login) is None


@pytest.mark.parametrize("leeway", [0, 5])
def test_a_revoked_chain_is_refused_until_its_longest_lived_token_is_refused_on_its_own(
    monkeypatch, leeway
):
    """The longest-lived token of the chain is one a peer whose clock runs `leeway` ahead minted
    as the chain was revoked, with the longest lifetime accepted here. One second before this
    service stops accepting it on its own, a revocation sweeps the table and the token is tried."""
    clock = Clock(monkeypatch, START)
    svc = _service(leeway_seconds=leeway)
    revoked_at = START
    iat = revoked_at + leeway
    lives = HOUR + leeway
    member = _peer_token(jti="m", iat=iat, lives=lives, cid="chain")
    unrevoked = _peer_token(jti="u", iat=iat, lives=lives)
    # The chain is revoked through another token of it, so the jti check cannot stand in for the
    # chain's when `member` is tried.
    assert svc.revoke_token(_peer_token(jti="s", iat=iat, lives=lives, cid="chain")) is True

    clock.at = iat + lives + leeway - 1
    svc.revoke_token(_peer_token(jti="sweep", iat=clock.at, lives=HOUR))

    assert svc.validate_token(unrevoked) is not None
    assert svc.validate_token(member) is None


# --- a chain another host keeps refreshing ------------------------------------------------------
#
# Revocations live in one process: a host sharing the key does not see this one's, and may go on
# refreshing the chain. It cannot refresh past the refresh cap, measured from the chain's `oiat`,
# so the revocation is held until a lifetime past that cap, or for ever when there is no cap.


def _peer_refreshes_a_chain_this_service_revoked(monkeypatch, **config):
    """Two services on one key: B logs alice in, A revokes that token, B refreshes it at +3,000 s.
    Returns the clock, A, B and B's refreshed token."""
    clock = Clock(monkeypatch, START)
    a, b = _service(**config), _service(**config)
    login = b.authenticate("alice", "s3cret-password")
    assert login is not None and a.revoke_token(login) is True

    clock.at = START + 3000
    refreshed = b.refresh_token(login)
    assert refreshed is not None
    assert a.validate_token(refreshed) is None  # while the lifetime-long hold would still run
    return clock, a, b, refreshed


@pytest.mark.parametrize("seconds", [3700, 6500, 9000])
def test_a_chain_another_host_refreshes_stays_revoked_here(monkeypatch, seconds):
    clock, a, b, refreshed = _peer_refreshes_a_chain_this_service_revoked(monkeypatch)
    if seconds == 9000:
        clock.at = START + 6000
        refreshed = b.refresh_token(refreshed)  # B refreshes again
        assert refreshed is not None

    clock.at = START + seconds
    a.revoke_token(_peer_token(jti="sweep", iat=clock.at, lives=HOUR))  # sweeps what has expired

    assert a.validate_token(_peer_token(jti="fresh", iat=clock.at - 60, lives=HOUR)) is not None
    if a.validate_token(refreshed) is not None:
        pytest.fail(f"the revoked chain's refreshed token is accepted at +{seconds} s")


def test_with_no_refresh_cap_a_revoked_chain_is_kept_past_any_finite_hold(monkeypatch):
    clock, a, b, refreshed = _peer_refreshes_a_chain_this_service_revoked(
        monkeypatch, refresh_max_lifetime_hours=None
    )
    # Past the longest finite hold the default cap would give (30 days and a lifetime), B has kept
    # refreshing the chain every half hour.
    for _ in range(4):
        clock.at += 1800
        refreshed = b.refresh_token(refreshed)
        assert refreshed is not None
    clock.at = START + 31 * 24 * HOUR
    refreshed = jwt.encode(
        {
            **jwt.decode(
                refreshed,
                SECRET,
                algorithms=["HS256"],
                options={"verify_exp": False, "verify_iat": False},
            ),
            "iat": clock.at - 60,
            "exp": clock.at - 60 + HOUR,
        },
        SECRET,
        algorithm="HS256",
    )
    a.revoke_token(_peer_token(jti="sweep", iat=clock.at, lives=HOUR))

    assert a.validate_token(_peer_token(jti="fresh", iat=clock.at - 60, lives=HOUR)) is not None
    assert a.validate_token(refreshed) is None
