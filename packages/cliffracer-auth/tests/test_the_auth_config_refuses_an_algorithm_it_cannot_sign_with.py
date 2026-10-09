"""`AuthConfig.algorithm` is one the service can sign and verify with its shared secret.

It was any string. `"none"`, `"RS256"` (an asymmetric algorithm, which needs a key pair this service
does not hold), a lower-case `"hs256"` and a typo were all accepted at construction, the service
started, and the first login raised `InvalidKeyError` or `NotImplementedError`. ADR-0010 puts that
refusal at startup, as for the config's other fields.

It is not a way around signature checking: a token signed with `none` never validated. This is about
where a wrong setting is found.
"""

import time

import jwt
import pytest
from cliffracer_auth import AuthConfig
from cliffracer_auth.simple_auth import SimpleAuthService
from pydantic import ValidationError

pytestmark = pytest.mark.unit

SECRET = "test-secret-not-a-real-one-0123456789abcdef"

# Written out, not read from the module: a test that took its list from the code it checks would pass
# for any list the code held.
HMAC_ALGORITHMS = ("HS256", "HS384", "HS512")


@pytest.mark.parametrize(
    "algorithm", ["none", "NONE", "RS256", "ES256", "hs256", "HS-256", "bogus", ""]
)
def test_an_algorithm_a_shared_secret_cannot_sign_with_is_refused_when_the_config_is_built(
    algorithm,
):
    with pytest.raises(ValidationError) as caught:
        AuthConfig(secret_key=SECRET, algorithm=algorithm)

    message = str(caught.value)
    assert "algorithm" in message
    assert all(name in message for name in HMAC_ALGORITHMS)


def test_the_refusal_applies_to_an_assignment_too():
    config = AuthConfig(secret_key=SECRET)

    with pytest.raises(ValidationError):
        config.algorithm = "none"

    assert config.algorithm == "HS256"


@pytest.mark.parametrize("algorithm", HMAC_ALGORITHMS)
def test_CONTROL_each_hmac_algorithm_signs_and_verifies(algorithm):
    auth = SimpleAuthService(AuthConfig(secret_key=SECRET, algorithm=algorithm))
    auth.create_user(username="alice", email="a@example.com", password="password123")

    token = auth.authenticate("alice", "password123")

    assert token is not None
    assert jwt.get_unverified_header(token)["alg"] == algorithm
    assert auth.validate_token(token) is not None


def test_CONTROL_the_default_is_hs256():
    assert AuthConfig(secret_key=SECRET).algorithm == "HS256"


def test_CONTROL_an_unsigned_token_does_not_validate():
    auth = SimpleAuthService(AuthConfig(secret_key=SECRET))
    forged = jwt.encode(
        {
            "jti": "x",
            "user_id": "u",
            "username": "mallory",
            "email": "m@x.io",
            "exp": time.time() + 600,
        },
        None,
        algorithm="none",
    )

    assert auth.validate_token(forged) is None


def test_the_documented_set_is_the_one_the_module_names():
    from cliffracer_auth.simple_auth import SIGNING_ALGORITHMS

    assert SIGNING_ALGORITHMS == HMAC_ALGORITHMS
