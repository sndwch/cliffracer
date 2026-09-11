"""Every user gets their own random salt, and old hashes keep verifying."""

import hashlib

import pytest
from cliffracer_auth.simple_auth import AuthConfig, AuthUser, SimpleAuthService

SECRET = "x" * 40


def _service(**overrides):
    return SimpleAuthService(AuthConfig(secret_key=SECRET, **overrides))


@pytest.mark.unit
class TestEncodedHash:
    def test_hash_has_the_encoded_shape(self):
        svc = _service()
        parts = svc.hash_password("correct horse battery").split("$")
        assert parts[0] == "pbkdf2_sha256"
        assert parts[1] == "100000"
        assert len(parts) == 4

    def test_the_same_password_hashes_differently_every_time(self):
        svc = _service()
        assert svc.hash_password("same") != svc.hash_password("same")

    def test_two_users_with_the_same_password_get_different_hashes(self):
        """Verify distinct users with identical passwords produce distinct hashes."""
        svc = _service()
        svc.create_user("alice", "alice@example.com", "shared-password")
        svc.create_user("bob", "bob@example.com", "shared-password")
        assert svc._users["alice"]["password_hash"] != svc._users["bob"]["password_hash"]

    def test_round_trip(self):
        svc = _service()
        h = svc.hash_password("correct horse battery")
        assert svc.verify_password("correct horse battery", h)
        assert not svc.verify_password("wrong", h)

    def test_iteration_count_is_configurable_and_encoded(self):
        svc = _service(pbkdf2_iterations=1000)
        h = svc.hash_password("pw")
        assert h.split("$")[1] == "1000"
        assert svc.verify_password("pw", h)

    def test_a_hash_verifies_against_its_own_encoded_iteration_count(self):
        """A hash made at 1000 iterations still verifies after the config moves on."""
        low = _service(pbkdf2_iterations=1000)
        h = low.hash_password("pw")
        high = _service(pbkdf2_iterations=200_000)
        assert high.verify_password("pw", h)


@pytest.mark.unit
class TestSecretKeyIndependence:
    def test_rotating_the_secret_key_does_not_invalidate_hashes(self):
        """Verify rotating the secret key does not invalidate password hashes."""
        svc = _service()
        svc.create_user("alice", "alice@example.com", "shared-password")
        stored = svc._users["alice"]["password_hash"]

        rotated = _service()
        rotated.config.secret_key = "y" * 40
        assert rotated.verify_password("shared-password", stored)


@pytest.mark.unit
class TestLegacyHashes:
    def _legacy(self, password, secret=SECRET):
        """Reproduce the pre-1.4.0 algorithm exactly."""
        salt = secret.encode()[:16]
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000).hex()

    def test_a_legacy_hash_still_verifies(self):
        """Nothing anyone stored via the public hash_password breaks."""
        svc = _service()
        assert svc.verify_password("legacy-password", self._legacy("legacy-password"))

    def test_a_legacy_hash_rejects_a_wrong_password(self):
        svc = _service()
        assert not svc.verify_password("wrong", self._legacy("legacy-password"))

    def test_new_hashes_are_never_written_in_the_legacy_form(self):
        svc = _service()
        assert svc.hash_password("pw").startswith("pbkdf2_sha256$")

    def test_a_legacy_hash_still_verifies_when_pbkdf2_iterations_has_moved_on(self):
        """The 100,000 iterations for legacy hashes must remain hardcoded for backwards compatibility."""
        svc = _service(pbkdf2_iterations=1000)
        legacy = self._legacy("legacy-password")
        assert svc.verify_password("legacy-password", legacy)


@pytest.mark.unit
class TestMalformedInput:
    @pytest.mark.parametrize(
        "bad",
        [
            "pbkdf2_sha256$100000$onlythree",
            "pbkdf2_sha256$notanumber$c2FsdA$aGFzaA",
            "pbkdf2_sha256$100000$!!!not-base64!!!$aGFzaA",
            "pbkdf2_sha256$100000$$",
            "",
            "$$$",
            "é",
            "pbkdf2_sha256$é$c2FsdA$aGFzaA",
            "pbkdf2_sha256$50000000$c2FsdA$aGFzaA",
            f"pbkdf2_sha256${'9' * 400}$c2FsdA$aGFzaA",
            None,
        ],
    )
    def test_malformed_hashes_return_false_and_do_not_raise(self, bad):
        """A corrupt stored record should fail authentication, not crash the service."""
        svc = _service()
        assert svc.verify_password("anything", bad) is False


_TRIPWIRE_MESSAGE = (
    "SimpleAuthService user store is not persistent across rebuilds. "
    "If persistence is implemented, upgrade legacy-form hashes on login."
)


@pytest.mark.unit
class TestUserStoreIsNotPersistent:
    """Verify that default SimpleAuthService user store is ephemeral across service instantiations."""

    def test_a_stored_user_does_not_survive_a_restart(self):
        config = AuthConfig(secret_key=SECRET)
        password = "tripwire-password"

        original = SimpleAuthService(config)
        original.create_user("tripwire", "tripwire@example.com", password)
        assert original.authenticate("tripwire", password) is not None, (
            "Setup failed: the user was never stored, or these credentials are "
            "wrong. Without this check the assertion below would pass for the "
            "wrong reason -- authenticate() returns None for an absent user too."
        )

        restarted = SimpleAuthService(config)

        assert restarted.authenticate("tripwire", password) is None, _TRIPWIRE_MESSAGE


@pytest.mark.unit
class TestLegacyPasswordRehashing:
    """Verify that authenticating with a legacy hash upgrades it to modern PBKDF2 (#339)."""

    def test_legacy_hash_upgraded_on_successful_login(self):
        config = AuthConfig(secret_key=SECRET)
        svc = SimpleAuthService(config)
        password = "legacy-secret-password-123"
        legacy_hash = svc._legacy_hash(password)

        user = AuthUser(
            user_id="user_legacy",
            username="legacy_user",
            email="legacy@example.com",
            roles={"user"},
        )
        svc._users["legacy_user"] = {"user": user, "password_hash": legacy_hash}

        assert not svc._users["legacy_user"]["password_hash"].startswith(f"{svc._HASH_PREFIX}$")
        assert svc._users["legacy_user"]["password_hash"] == legacy_hash

        # First authentication: succeeds and transparently upgrades hash
        token = svc.authenticate("legacy_user", password)
        assert token is not None

        upgraded_hash = svc._users["legacy_user"]["password_hash"]
        assert upgraded_hash.startswith(f"{svc._HASH_PREFIX}$")
        assert upgraded_hash != legacy_hash

        # Second authentication: verifies against the upgraded hash without re-upgrading
        token2 = svc.authenticate("legacy_user", password)
        assert token2 is not None
        assert svc._users["legacy_user"]["password_hash"] == upgraded_hash

    def test_failed_login_does_not_upgrade_legacy_hash(self):
        config = AuthConfig(secret_key=SECRET)
        svc = SimpleAuthService(config)
        legacy_hash = svc._legacy_hash("correct-password")

        user = AuthUser(
            user_id="user_legacy",
            username="legacy_user",
            email="legacy@example.com",
        )
        svc._users["legacy_user"] = {"user": user, "password_hash": legacy_hash}

        # Authentication with wrong password fails and hash is untouched
        assert svc.authenticate("legacy_user", "wrong-password") is None
        assert svc._users["legacy_user"]["password_hash"] == legacy_hash

    def test_inactive_user_does_not_upgrade_legacy_hash(self):
        config = AuthConfig(secret_key=SECRET)
        svc = SimpleAuthService(config)
        legacy_hash = svc._legacy_hash("correct-password")

        user = AuthUser(
            user_id="user_inactive",
            username="inactive_user",
            email="inactive@example.com",
            is_active=False,
        )
        svc._users["inactive_user"] = {"user": user, "password_hash": legacy_hash}

        # Inactive user cannot authenticate; hash remains untouched
        assert svc.authenticate("inactive_user", "correct-password") is None
        assert svc._users["inactive_user"]["password_hash"] == legacy_hash

    def test_modern_hash_is_not_modified_on_login(self):
        svc = _service()
        svc.create_user("modern", "modern@example.com", "modern-password")
        original_hash = svc._users["modern"]["password_hash"]
        assert original_hash.startswith(f"{svc._HASH_PREFIX}$")

        token = svc.authenticate("modern", "modern-password")
        assert token is not None
        assert svc._users["modern"]["password_hash"] == original_hash


@pytest.mark.unit
class TestAuthConfigCleanups:
    """Verify pruning of vestigial fields from AuthConfig (#338)."""

    def test_bcrypt_rounds_is_not_a_valid_field(self):
        assert "bcrypt_rounds" not in AuthConfig.model_fields
        config = AuthConfig(secret_key=SECRET)
        assert not hasattr(config, "bcrypt_rounds")
