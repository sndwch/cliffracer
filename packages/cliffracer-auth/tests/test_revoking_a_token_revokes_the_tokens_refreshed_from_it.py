"""Revoking a token reaches the tokens refreshed from it, and the ones it was refreshed from.

A refresh mints a new `jti`, so a revocation keyed on the `jti` alone is defeated by one
prior refresh: the child of a revoked token validates and can be refreshed again, for as long
as the lifetime cap allows. The tokens one login and its refreshes make share a chain id, the
`cid` claim, and revoking any one of them revokes the chain.
"""

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

pytestmark = pytest.mark.unit

SECRET = "x" * 40


def _service(**overrides):
    svc = SimpleAuthService(AuthConfig(secret_key=SECRET, **overrides))
    svc.create_user("alice", "alice@example.com", "s3cret-password")
    return svc


def _login(svc):
    token = svc.authenticate("alice", "s3cret-password")
    assert token is not None
    return token


def _decode(svc, token):
    return jwt.decode(token, SECRET, algorithms=[svc.config.algorithm])


def _hand_signed(svc, *, jti, lives, **extra):
    """A token the service did not mint, with no `cid`: the shape of one signed before chains."""
    now = datetime.now(UTC)
    claims = {
        "jti": jti,
        "user_id": "user_1",
        "username": "alice",
        "email": "alice@example.com",
        "exp": (now + timedelta(seconds=lives)).timestamp(),
        "iat": (now - timedelta(minutes=5)).timestamp(),
        "oiat": (now - timedelta(minutes=5)).timestamp(),
        **extra,
    }
    return jwt.encode(claims, SECRET, algorithm=svc.config.algorithm)


def test_the_children_of_a_revoked_token_are_refused_and_cannot_be_refreshed():
    svc = _service()
    parent = _login(svc)
    child = svc.refresh_token(parent)
    grandchild = svc.refresh_token(child)
    assert child is not None and grandchild is not None
    assert svc.validate_token(child) is not None, "the child is live before the revocation"

    assert svc.revoke_token(parent) is True

    assert svc.validate_token(parent) is None
    assert svc.validate_token(child) is None
    assert svc.validate_token(grandchild) is None
    assert svc.refresh_token(child) is None


def test_revoking_the_newest_token_refuses_the_ones_it_was_refreshed_from():
    svc = _service()
    parent = _login(svc)
    child = svc.refresh_token(parent)
    assert child is not None

    assert svc.revoke_token(child) is True

    assert svc.validate_token(parent) is None
    assert svc.refresh_token(parent) is None


def test_a_separate_login_is_a_separate_chain_and_is_untouched():
    svc = _service()
    first = _login(svc)
    other = _login(svc)
    other_child = svc.refresh_token(other)
    assert other_child is not None

    svc.revoke_token(first)

    assert svc.validate_token(other) is not None
    assert svc.validate_token(other_child) is not None


def test_a_token_signed_before_chains_existed_names_its_chain_by_its_jti():
    svc = _service()
    legacy = _hand_signed(svc, jti="legacy-jti", lives=600)
    child = svc.refresh_token(legacy)
    assert child is not None
    assert _decode(svc, child)["cid"] == "legacy-jti"
    assert svc.validate_token(child) is not None

    assert svc.revoke_token(legacy) is True

    assert svc.validate_token(child) is None


def test_an_expired_parent_still_revokes_the_children_that_are_live():
    svc = _service()
    parent = _hand_signed(svc, jti="expired-parent", lives=-60)
    alice = svc._stored("alice")["user"]
    child = svc._mint_token(alice, chain_id="expired-parent")
    assert svc.validate_token(child) is not None

    assert svc.revoke_token(parent) is True

    assert svc.validate_token(child) is None


def test_the_chain_id_is_carried_across_refreshes_and_differs_between_logins():
    svc = _service()
    first = _login(svc)
    second = _login(svc)
    refreshed = svc.refresh_token(svc.refresh_token(first))

    first_claims, second_claims = _decode(svc, first), _decode(svc, second)
    assert first_claims["cid"] == first_claims["jti"], "a login starts a chain named by its jti"
    assert _decode(svc, refreshed)["cid"] == first_claims["cid"]
    assert second_claims["cid"] != first_claims["cid"]


def test_a_cid_of_the_wrong_type_is_refused_and_revokes_nothing():
    svc = _service()
    bad = _hand_signed(svc, jti="bad-cid", lives=600, cid=123)

    assert svc.validate_token(bad) is None
    assert svc.revoke_token(bad) is False
    assert svc._revoked_jtis == {} and svc._revoked_chains == {}


def test_revoking_a_chain_twice_still_reports_it_revoked():
    svc = _service()
    parent = _login(svc)
    child = svc.refresh_token(parent)

    assert svc.revoke_token(parent) is True
    assert svc.revoke_token(child) is True
    assert len(svc._revoked_chains) == 1


def test_a_revoked_chain_is_forgotten_once_no_token_of_it_can_be_refreshed_or_accepted(
    monkeypatch,
):
    """Another host sharing the key may refresh the chain until its refresh cap, and the last token
    it mints lives a lifetime past that: here a 1 h cap and a 1 h lifetime, so 2 h and the second
    of rounding after the login."""
    svc = _service(token_expiry_hours=1, refresh_max_lifetime_hours=1)
    gone = _login(svc)
    svc.revoke_token(gone)
    chain = _decode(svc, gone)["cid"]
    assert chain in svc._revoked_chains

    later = time.time() + 2 * 3600 + 2
    monkeypatch.setattr("cliffracer_auth.simple_auth.time", SimpleNamespace(time=lambda: later))
    kept = _login(svc)
    svc.revoke_token(kept)

    assert chain not in svc._revoked_chains
    assert _decode(svc, kept)["cid"] in svc._revoked_chains


def test_a_chain_outlives_the_parent_token_that_revoked_it(monkeypatch):
    """A child minted late lives past its parent's `exp`; the chain must be kept for it."""
    svc = _service(token_expiry_hours=1)
    parent = _hand_signed(svc, jti="short-parent", lives=600)
    child = svc._mint_token(svc._stored("alice")["user"], chain_id="short-parent")
    svc.revoke_token(parent)

    halfway = time.time() + 1800  # the parent has expired; the child has half an hour left
    monkeypatch.setattr("cliffracer_auth.simple_auth.time", SimpleNamespace(time=lambda: halfway))
    svc.revoke_token(_login(svc))  # any revocation prunes what is past its time

    assert "short-parent" in svc._revoked_chains
    assert svc.validate_token(child) is None


def test_CONTROL_a_chain_is_kept_for_the_whole_lifetime_of_the_tokens_it_could_hold(monkeypatch):
    svc = _service(token_expiry_hours=1)
    kept = _login(svc)
    svc.revoke_token(kept)
    chain = _decode(svc, kept)["cid"]

    sooner = time.time() + 1800
    monkeypatch.setattr("cliffracer_auth.simple_auth.time", SimpleNamespace(time=lambda: sooner))
    svc.revoke_token(_login(svc))

    assert chain in svc._revoked_chains
