"""`AuthConfig.leeway_seconds` forgives clock skew on `iat` and `exp`, and defaults to none.

In a fleet every service verifies tokens minted elsewhere, so a second of skew between the host that
minted a token and the host that verifies it refused a valid login as "not yet valid". The leeway
applies to `iat` and to `exp`, so it also keeps every token alive that many seconds past its expiry,
and a revocation has to hold for that long too.
"""

import time

import jwt
import pytest
from cliffracer_auth.simple_auth import AuthConfig, SimpleAuthService

pytestmark = pytest.mark.unit

SECRET = "the-signing-key-" + "k" * 24


def _service(**config) -> SimpleAuthService:
    return SimpleAuthService(AuthConfig(secret_key=SECRET, **config))


def _token(*, iat_offset: float = 0.0, exp_offset: float = 3600.0, jti: str = "j1") -> str:
    now = time.time()
    claims = {
        "jti": jti,
        "user_id": "u1",
        "username": "bob",
        "email": "bob@example.com",
        "iat": now + iat_offset,
        "exp": now + exp_offset,
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def test_the_default_is_no_leeway():
    assert AuthConfig(secret_key=SECRET).leeway_seconds == 0.0


def test_a_negative_leeway_is_refused():
    with pytest.raises(ValueError, match="leeway_seconds"):
        AuthConfig(secret_key=SECRET, leeway_seconds=-1)


@pytest.mark.parametrize("not_finite", [float("inf"), float("-inf"), float("nan")])
def test_a_leeway_that_is_not_a_finite_number_is_refused(not_finite):
    """An infinite leeway makes every token valid for ever, and a revocation would then have to be
    kept for ever too."""
    with pytest.raises(ValueError, match="leeway_seconds"):
        AuthConfig(secret_key=SECRET, leeway_seconds=not_finite)


def test_assigning_a_leeway_that_is_not_finite_is_refused_too():
    config = AuthConfig(secret_key=SECRET, leeway_seconds=5)

    with pytest.raises(ValueError, match="leeway_seconds"):
        config.leeway_seconds = float("inf")


# ---- iat: a token minted by a host whose clock is ahead ---------------------------------------


def test_a_token_minted_two_seconds_ahead_is_refused_with_no_leeway():
    assert _service().validate_token(_token(iat_offset=2)) is None


def test_the_same_token_is_accepted_with_a_leeway_that_covers_the_skew():
    context = _service(leeway_seconds=5).validate_token(_token(iat_offset=2))

    assert context is not None and context.user is not None
    assert context.user.username == "bob"


def test_a_skew_larger_than_the_leeway_is_still_refused():
    assert _service(leeway_seconds=5).validate_token(_token(iat_offset=30)) is None


# ---- exp: the leeway extends every token's life -----------------------------------------------


def test_a_token_that_expired_a_second_ago_is_refused_with_no_leeway():
    assert _service().validate_token(_token(exp_offset=-1)) is None


def test_the_same_token_is_accepted_inside_the_leeway_and_refused_beyond_it():
    svc = _service(leeway_seconds=5)

    assert svc.validate_token(_token(exp_offset=-1)) is not None
    assert svc.validate_token(_token(exp_offset=-20)) is None


def test_a_token_the_service_minted_is_unaffected_by_a_leeway():
    svc = _service(leeway_seconds=5)
    svc.create_user("alice", "alice@example.com", "a-long-enough-password")
    token = svc.authenticate("alice", "a-long-enough-password")
    assert token is not None

    context = svc.validate_token(token)

    assert context is not None and context.user is not None and context.user.username == "alice"


# ---- revocation and refresh read the same leeway ----------------------------------------------


def test_a_token_revoked_inside_its_leeway_stays_revoked():
    """Accepted for 5 seconds past `exp`, so a revocation made then must hold for as long."""
    svc = _service(leeway_seconds=5)
    token = _token(exp_offset=-1)
    assert svc.validate_token(token) is not None

    assert svc.revoke_token(token) is True

    assert svc.validate_token(token) is None
    # Held by the token's own entry, not only by its chain: the entry is what a token that carries
    # no chain id is checked against.
    assert "j1" in svc._revoked_jtis


def test_a_revocation_is_kept_for_the_leeway_past_the_tokens_exp():
    svc = _service(leeway_seconds=5)
    token = _token(exp_offset=60)
    exp = jwt.decode(token, SECRET, algorithms=["HS256"])["exp"]

    svc.revoke_token(token)

    assert svc._revoked_jtis["j1"] == pytest.approx(exp + 5, abs=0.001)
    # And the chain: a token of it accepted here was issued at most the leeway ahead, lives at
    # most the lifetime plus the leeway (and a second of rounding), and is accepted the leeway past
    # its exp, so the chain holds three leeways and the second past the lifetime.
    chain_until = svc._revoked_chains["j1"]
    assert chain_until == pytest.approx(time.time() + 24 * 3600 + 3 * 5 + 1, abs=1)


def test_with_no_leeway_a_token_expired_a_second_ago_is_nothing_to_revoke():
    svc = _service()

    assert svc.revoke_token(_token(exp_offset=-1)) is True
    assert svc._revoked_jtis == {}


def test_a_token_inside_its_leeway_can_be_refreshed():
    svc = _service(leeway_seconds=5)
    svc.create_user("alice", "alice@example.com", "a-long-enough-password")
    token = svc.authenticate("alice", "a-long-enough-password")
    assert token is not None
    payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    just_expired = jwt.encode({**payload, "exp": time.time() - 1}, SECRET, algorithm="HS256")

    refreshed = svc.refresh_token(just_expired)

    assert refreshed is not None
    assert svc.validate_token(refreshed) is not None


def test_a_token_a_second_past_expiry_is_not_refreshed_with_no_leeway():
    svc = _service()
    svc.create_user("alice", "alice@example.com", "a-long-enough-password")
    token = svc.authenticate("alice", "a-long-enough-password")
    assert token is not None
    payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    just_expired = jwt.encode({**payload, "exp": time.time() - 1}, SECRET, algorithm="HS256")

    assert svc.refresh_token(just_expired) is None
