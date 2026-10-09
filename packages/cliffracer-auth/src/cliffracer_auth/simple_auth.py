"""JWT token issuance, verification, and handler authorization.

Provides AuthConfig, token signing with HS256, password hashing, and
auth context management for RPC and listener handlers.
"""

import base64
import binascii
import functools
import hashlib
import hmac
import inspect
import math
import secrets
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

import jwt
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

# Import canonical AuthenticationError and AuthorizationError from core.
from cliffracer.core.decorators import refuse_bare_use
from cliffracer.core.exceptions import AuthenticationError, AuthorizationError, ConfigurationError
from cliffracer.core.validation import normalize_username

#: The two bounds `AuthConfig` and `verify_password` share, so a configured count can never create
#: a user the verifier then refuses.
#:
#: The fewest PBKDF2 iterations `verify_password` accepts in a stored record, which is also the
#: fewest `AuthConfig` lets a service be configured for. It is NIST SP 800-132's stated minimum and
#: the lowest count anything here runs; the default of 100,000 is what a record is upgraded to.
#: A record below it is refused, and its user's password has to be set again.
MIN_PBKDF2_ITERATIONS = 1_000
#: The most PBKDF2 iterations `verify_password` will run for a stored record. A configured count
#: above it would create users it then refuses to verify, so `AuthConfig` bounds the field to it.
MAX_PBKDF2_ITERATIONS = 10_000_000  # ~2s per verify at this ceiling; caps attacker-supplied cost

#: The algorithms `AuthConfig.algorithm` accepts: the HMAC family, which signs and verifies with the
#: shared `secret_key`. An asymmetric algorithm needs a key pair this service does not hold, and
#: `none` signs nothing.
SIGNING_ALGORITHMS = ("HS256", "HS384", "HS512")

#: The default for `AuthConfig.refresh_max_lifetime_hours`: 30 days, thirty times the default
#: `token_expiry_hours` of 24. A session longer than a month needs a new login.
DEFAULT_REFRESH_MAX_LIFETIME_HOURS = 30 * 24

# Context variable for storing auth context
auth_context_var: ContextVar[Optional["AuthContext"]] = ContextVar("auth_context", default=None)

#: Seconds allowed on top of `token_expiry_hours` when a token's lifetime is measured: `exp` minus
#: `iat` of a token this service minted differs from the lifetime by float rounding.
_LIFETIME_ROUNDING_SECONDS = 1.0


class AuthConfig(BaseModel):
    """Configuration for authentication system.

    A field the class does not have is refused, as `ServiceConfig` refuses one: a misspelt
    `pbkdf2_iteration` would otherwise leave an operator who believes they raised the hash
    cost running at the default.

    `secret_key` is a `SecretStr`: printing, logging or dumping the config shows a mask, and the
    key is read with `config.secret_key.get_secret_value()`. A `str` is accepted wherever the
    field is set, at construction and by assignment.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @model_validator(mode="before")
    @classmethod
    def _refuse_enable_auth_by_name(cls, data: Any) -> Any:
        if isinstance(data, dict) and "enable_auth" in data:
            raise ValueError(
                "AuthConfig has no `enable_auth` field: declaring `AuthExtension` on the "
                "service is what turns authentication on"
            )
        return data

    @field_validator("algorithm")
    @classmethod
    def _refuse_an_algorithm_a_shared_secret_cannot_sign_with(cls, value: str) -> str:
        if value not in SIGNING_ALGORITHMS:
            raise ValueError(
                f"algorithm must be one of {', '.join(SIGNING_ALGORITHMS)} (the HMAC algorithms a "
                f"shared secret_key signs with), got {value!r}"
            )
        return value

    secret_key: SecretStr = Field(..., description="Secret key for JWT signing")
    algorithm: str = Field(
        default="HS256",
        description=(
            "JWT signing algorithm: one of HS256, HS384 or HS512, the HMAC family a shared "
            "`secret_key` signs with. Any other value is refused when the config is built."
        ),
    )
    token_expiry_hours: int = Field(
        default=24,
        description=(
            "Token expiry in hours, which is also the longest lifetime (`exp` minus `iat`) a token "
            "this service accepts may have: every host sharing the key must use this value (and "
            "this `refresh_max_lifetime_hours`) or a smaller one, and a token that lives longer "
            "is refused. Those bounds are what keep a revoked chain revoked for as long as any "
            "token of it can be accepted, whichever host refreshes it."
        ),
    )
    pbkdf2_iterations: int = Field(
        default=100_000,
        ge=MIN_PBKDF2_ITERATIONS,
        le=MAX_PBKDF2_ITERATIONS,
        description="PBKDF2 iterations for new password hashes",
    )
    leeway_seconds: float = Field(
        default=0.0,
        ge=0,
        allow_inf_nan=False,
        description=(
            "Seconds of clock skew a token may be off by. Applied to `iat` (a token minted by a "
            "host whose clock is ahead is not refused as not yet valid) and to `exp`, so it also "
            "extends every token's life by this many seconds: an expired token is accepted for "
            "that long. 0 accepts nothing the clocks disagree about."
        ),
    )
    refresh_max_lifetime_hours: int | None = Field(
        default=DEFAULT_REFRESH_MAX_LIFETIME_HOURS,
        description=(
            "Cap on total lifetime from the original login, across refreshes: a token is no "
            "longer refreshed once this many hours have passed since the login that began its "
            "chain, so it bounds how long a leaked token can be kept alive by refreshing it. "
            "Refresh is a re-issue: the token it was given stays valid to its own expiry. None "
            "removes the cap, and then a single login grants access indefinitely."
        ),
    )


@dataclass
class AuthUser:
    """Authenticated user information.

    The `AuthUser` a handler receives from a token is a projection: `user_id`, `username`,
    `email`, `roles` and `permissions` come from the token's claims, while `created_at` is the
    time the token was validated and `is_active` is always True (a token for a deactivated user
    is refused, so a handler never sees one). The stored record, with the real `created_at`,
    stays in the service's store.
    """

    user_id: str
    username: str
    email: str
    roles: set[str] = field(default_factory=set)
    permissions: set[str] = field(default_factory=set)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    is_active: bool = True


@dataclass
class AuthContext:
    """Current authentication context

    `expires_at` is when the context stops being valid. For a context `validate_token` built it is
    the token's `exp` plus `AuthConfig.leeway_seconds`, the last moment the token is accepted.
    """

    user: AuthUser | None = None
    token: str | None = None
    expires_at: datetime | None = None

    @property
    def is_authenticated(self) -> bool:
        """Check if context is authenticated"""
        return self.user is not None and self.is_valid

    @property
    def is_valid(self) -> bool:
        """Check if auth is still valid"""
        if not self.expires_at:
            return False
        return datetime.now(UTC) < self.expires_at


def _claims_have_their_types(payload: dict[str, Any]) -> bool:
    """Whether `user_id`, `username` and `email` are strings and `roles` and `permissions`, where
    present, are lists of strings. A missing identity claim raises `KeyError`."""
    user_id, username, email = payload["user_id"], payload["username"], payload["email"]
    if not (isinstance(user_id, str) and isinstance(username, str) and isinstance(email, str)):
        return False
    for claim in (payload.get("roles", []), payload.get("permissions", [])):
        if not isinstance(claim, list):
            return False
        for item in claim:
            if not isinstance(item, str):
                return False
    return True


class SimpleAuthService:
    """In-memory authentication and JWT issuing service.

    Maintains an in-memory user registry and verifies HMAC SHA-256 tokens.
    State is stored in memory and reset across service restarts.
    """

    def __init__(self, config: AuthConfig):
        self.config = config
        self._users: dict[str, dict] = {}  # In-memory user store
        self._users_created = 0  # ids come from this, never from len(_users), which can shrink
        self._unknown_user_hash: str | None = None
        # jti -> the token's `exp` plus the leeway. A revocation is kept only until the token
        # would have expired anyway: after that the token is refused on its own, and the entry is
        # dead weight.
        self._revoked_jtis: dict[str, float] = {}
        # chain id -> when the revocation can be forgotten. A refresh mints a new jti, so
        # revoking one token's jti leaves its descendants valid; the chain is what they share.
        self._revoked_chains: dict[str, float] = {}

        if len(config.secret_key.get_secret_value()) < 32:
            raise ValueError("Secret key must be at least 32 characters")

    def _decode(self, token: str, *, options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Decode and verify `token` with this service's key and the configured leeway."""
        payload: dict[str, Any] = jwt.decode(
            token,
            self._signing_key(),
            algorithms=[self.config.algorithm],
            leeway=self.config.leeway_seconds,
            options=options,
        )
        return payload

    def _longest_lifetime(self) -> float:
        """The longest `exp` minus `iat` a token accepted here may have, in seconds."""
        return (
            self.config.token_expiry_hours * 3600
            + self.config.leeway_seconds
            + _LIFETIME_ROUNDING_SECONDS
        )

    def _chain_hold(self, payload: dict[str, Any], now: float) -> float:
        """When a chain revoked now can be forgotten: after the last token of it can be accepted.

        Kept even when the revoked token has expired: tokens refreshed from it may be live. A
        token of the chain accepted here was issued at most `leeway_seconds` after its minting
        host's clock reads (a later `iat` is refused), lives at most `_longest_lifetime()`, and is
        accepted `leeway_seconds` past its `exp`. Another host sharing the key does not see this
        revocation and may go on refreshing the chain, but not past the refresh cap, measured
        from the chain's `oiat`: so the hold runs to the later of a lifetime from now and a
        lifetime past that cap. With no cap (`refresh_max_lifetime_hours=None`) a chain can be
        refreshed for ever, and its revocation is kept for the life of the process. A token with
        no usable `oiat` cannot be refreshed by this library, so a lifetime from now holds it.
        """
        margin = self._longest_lifetime() + 2 * self.config.leeway_seconds
        hold = now + margin
        oiat = payload.get("oiat")
        if isinstance(oiat, bool) or not isinstance(oiat, int | float):
            return hold
        cap = self.config.refresh_max_lifetime_hours
        if cap is None:
            return math.inf
        return max(hold, oiat + cap * 3600 + margin)

    def _signing_key(self) -> str:
        """The key tokens are signed and verified with, read from the config at the moment of use."""
        return self.config.secret_key.get_secret_value()

    _HASH_PREFIX = "pbkdf2_sha256"
    _MIN_ITERATIONS = MIN_PBKDF2_ITERATIONS
    _MAX_ITERATIONS = MAX_PBKDF2_ITERATIONS

    @staticmethod
    def _b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _unb64(text: str) -> bytes:
        padding = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(text + padding)

    def hash_password(self, password: str) -> str:
        """Hash a password with a fresh random per-user salt.

        Returns ``pbkdf2_sha256$<iterations>$<salt>$<hash>``, salt and hash
        URL-safe base64 without padding. The encoded form keeps this method's
        one-argument signature while carrying the salt, and carrying the
        iteration count makes it upgradeable later without another break.
        """
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, self.config.pbkdf2_iterations
        )
        return (
            f"{self._HASH_PREFIX}${self.config.pbkdf2_iterations}$"
            f"{self._b64(salt)}${self._b64(digest)}"
        )

    def _legacy_hash(self, password: str) -> str:
        """Deprecated legacy hash verification retained for backward compatibility."""
        salt = self._signing_key().encode()[:16]
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000).hex()

    def verify_password(self, password: str, password_hash: str) -> bool:
        """Verify a password against either hash form.

        Malformed input returns False rather than raising: a corrupt stored
        record should fail authentication, not take the service down. This
        covers non-ASCII or non-``str`` stored records and iteration counts
        outside a sane range, not just bad base64/field-count. A count below
        `MIN_PBKDF2_ITERATIONS` is refused with a warning that names it.
        """
        if not isinstance(password_hash, str):
            return False  # type: ignore[unreachable]

        if not password_hash.startswith(f"{self._HASH_PREFIX}$"):
            try:
                legacy = self._legacy_hash(password)
                return hmac.compare_digest(legacy.encode(), password_hash.encode())
            except (ValueError, TypeError, UnicodeEncodeError):
                return False

        try:
            _, iterations_str, salt_b64, digest_b64 = password_hash.split("$")
            iterations = int(iterations_str)
            if not (0 < iterations <= self._MAX_ITERATIONS):
                return False
            if iterations < self._MIN_ITERATIONS:
                return self._refuse_a_record_below_the_floor(password, iterations)
            salt = self._unb64(salt_b64)
            expected = self._unb64(digest_b64)
            if not salt or not expected:
                return False
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        except (ValueError, TypeError, OverflowError, binascii.Error):
            return False

        return hmac.compare_digest(candidate, expected)

    def _refuse_a_record_below_the_floor(self, password: str, iterations: int) -> bool:
        """Refuse a record that offers too little work, saying so and costing what a verify costs.

        `verify_password` returns False and cannot raise, so the reason is the log line, which
        names the record's count and the floor. One PBKDF2 at the configured count is spent, as for
        a name that is not registered, so the time a login takes does not say which users hold a
        record this weak.
        """
        logger.warning(
            f"Refused a stored password record made at {iterations} PBKDF2 iterations, below "
            f"the floor of {self._MIN_ITERATIONS}; the user's password has to be set again"
        )
        hashlib.pbkdf2_hmac("sha256", password.encode(), bytes(16), self.config.pbkdf2_iterations)
        return False

    def _needs_rehash(self, password_hash: str) -> bool:
        """Whether a record that just verified should be written again at today's cost.

        True for the legacy form, and for a PBKDF2 record made at fewer iterations than this
        service is now configured for: the count is carried in the record so that a stronger
        configuration upgrades what was stored under a weaker one at the next login.
        """
        if not password_hash.startswith(f"{self._HASH_PREFIX}$"):
            return True
        try:
            return int(password_hash.split("$")[1]) < self.config.pbkdf2_iterations
        except (IndexError, ValueError):
            return True

    @staticmethod
    def _key(username: str) -> str:
        """The key a user is stored under, however the name was typed."""
        return normalize_username(username)

    def _stored(self, username: str) -> dict[str, Any]:
        """The stored user, or `ValueError` naming the name as typed."""
        user_data = self._users.get(self._key(username))
        if user_data is None:
            raise ValueError(f"User {username} does not exist")
        return user_data

    def create_user(
        self,
        username: str,
        email: str,
        password: str,
        roles: set[str] | None = None,
        permissions: set[str] | None = None,
    ) -> AuthUser:
        """Create a new user"""
        # Absolute since the move: this file is no longer inside cliffracer.
        # A FUNCTION-LOCAL import, which a top-of-file import grep does not
        # see -- it was found by a test, not by reading.
        from cliffracer.core.validation import (
            validate_password,
            validate_string_length,
            validate_username,
        )

        # Validate inputs
        username = validate_username(username)
        password = validate_password(password)
        email = validate_string_length(email, min_length=3, max_length=254, field_name="Email")

        # Basic email validation
        if "@" not in email or "." not in email.split("@")[1]:
            raise ValueError("Invalid email format")

        if username in self._users:
            raise ValueError(f"User {username} already exists")

        self._users_created += 1
        user_id = f"user_{self._users_created}"
        user = AuthUser(
            user_id=user_id,
            username=username,
            email=email,
            roles=set(roles or ()),
            permissions=set(permissions or ()),
        )

        self._users[username] = {"user": user, "password_hash": self.hash_password(password)}

        logger.info(f"Created user: {username}")
        return user

    def _mint_token(
        self,
        user: AuthUser,
        original_iat: float | None = None,
        chain_id: str | None = None,
    ) -> str:
        """Sign a JWT for a user. The only place a token is created.

        ``oiat`` is the original issue time, carried unchanged across refreshes
        so ``refresh_max_lifetime_hours`` measures from the original login
        rather than restarting on every refresh. It is framework-internal.

        ``cid`` names the chain of tokens one login and its refreshes make: a login
        starts a chain whose id is its own ``jti``, and a refresh carries it on.
        Revoking any token of a chain revokes the chain.
        """
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=self.config.token_expiry_hours)
        issued_at = now.timestamp()
        jti = secrets.token_hex(16)
        payload = {
            "jti": jti,
            "cid": chain_id if chain_id is not None else jti,
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "roles": list(user.roles),
            "permissions": list(user.permissions),
            "exp": expires_at.timestamp(),
            "iat": issued_at,
            "oiat": original_iat if original_iat is not None else issued_at,
        }
        return jwt.encode(payload, self._signing_key(), algorithm=self.config.algorithm)

    def authenticate(self, username: str, password: str) -> str | None:
        """Authenticate user and return JWT token"""
        user_data = self._users.get(self._key(username))
        if not user_data:
            # A user that does not exist costs the same PBKDF2 as one that does, so the time
            # taken does not say which names are registered.
            if self._unknown_user_hash is None:
                self._unknown_user_hash = self.hash_password(secrets.token_hex(16))
            self.verify_password(password, self._unknown_user_hash)
            logger.warning(f"Authentication failed: user {username} not found")
            return None

        if not self.verify_password(password, user_data["password_hash"]):
            logger.warning(f"Authentication failed: invalid password for {username}")
            return None

        user = user_data["user"]
        if not user.is_active:
            logger.warning(f"Authentication failed: user {username} is inactive")
            return None

        if self._needs_rehash(user_data["password_hash"]):
            user_data["password_hash"] = self.hash_password(password)
            logger.info(f"Upgraded password hash for user {username}")

        token = self._mint_token(user)
        logger.info(f"User {username} authenticated successfully")
        return token

    def validate_token(self, token: str) -> AuthContext | None:
        """Validate JWT token and return auth context.

        Returns None for anything that is not a valid token of this service: an expired,
        revoked or undecodable one, one that lacks a required claim (`exp`, `jti`, `user_id`,
        `username`, `email`, `iat`), lives longer than `token_expiry_hours` (`exp` minus `iat`),
        carries one of the wrong type, or carries a `user_id`, `username`
        or `email` that is empty or whitespace alone, and one for a user this service holds as
        inactive. A token for a user this service has no record of is accepted: the user store
        is in memory, and a token may come from another issuer sharing the key.
        """
        validated = self._validated(token)
        return validated[0] if validated is not None else None

    def _validated(self, token: str) -> tuple[AuthContext, dict[str, Any]] | None:
        """`validate_token`'s decision, with the payload it was made on, so a caller that needs
        a claim does not verify the signature a second time."""
        try:
            payload = self._decode(
                token, options={"require": ["exp", "iat", "jti", "user_id", "username", "email"]}
            )

            # A token that lives longer than this service's own could outlive a chain revocation,
            # which is held for this service's longest lifetime (`revoke_token`), so it is refused.
            if payload["exp"] - payload["iat"] > self._longest_lifetime():
                logger.warning(
                    "Token validation failed: it lives longer than this service's "
                    f"token_expiry_hours ({self.config.token_expiry_hours}h)"
                )
                return None

            # Revocation is keyed on the jti, so a token without one could never
            # be revoked; it is refused rather than accepted until it expires.
            jti = payload["jti"]
            if not jti:
                logger.warning("Token validation failed: no jti claim, so it cannot be revoked")
                return None
            if jti in self._revoked_jtis:
                logger.warning(f"Token validation failed: token has been revoked (jti={jti})")
                return None
            # A token minted before chains existed has none: its own jti names it.
            chain = payload.get("cid", jti)
            if not isinstance(chain, str) or not chain:
                logger.warning("Token validation failed: the cid claim has the wrong type")
                return None
            if chain in self._revoked_chains:
                logger.warning(f"Token validation failed: its chain was revoked (jti={jti})")
                return None

            # Reconstruct user from payload. A claim of the wrong type is not a valid token.
            if not _claims_have_their_types(payload):
                logger.warning("Token validation failed: a claim has the wrong type")
                return None
            roles = payload.get("roles", [])
            permissions = payload.get("permissions", [])

            stored = self._users.get(self._key(payload["username"]))
            if stored is not None and not stored["user"].is_active:
                logger.warning(f"Token validation failed: user {payload['username']} is inactive")
                return None

            # This service never mints an identity claim that is empty or whitespace alone: a
            # username is three or more letters, digits, `_`, `-` or `.`, an email has an `@` and a
            # `.` after it, and the user id is generated.
            if not (
                payload["user_id"].strip()
                and payload["username"].strip()
                and payload["email"].strip()
            ):
                logger.warning("Token validation failed: an identity claim is empty or blank")
                return None

            user = AuthUser(
                user_id=payload["user_id"],
                username=payload["username"],
                email=payload["email"],
                roles=set(roles),
                permissions=set(permissions),
            )

            # Create auth context
            # The token is accepted for `leeway_seconds` past its `exp`, so the context is valid for
            # as long: `is_valid`, which `AuthExtension` and `requires_auth` read, has no leeway of
            # its own to add.
            context = AuthContext(
                user=user,
                token=token,
                expires_at=datetime.fromtimestamp(payload["exp"] + self.config.leeway_seconds, UTC),
            )

            return context, payload

        except jwt.ExpiredSignatureError:
            logger.warning("Token validation failed: expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"Token validation failed: {e}")
            return None
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as e:
            logger.warning(f"Token validation failed: malformed claims ({type(e).__name__}: {e})")
            return None

    def refresh_token(self, token: str) -> str | None:
        """Issue a new token from a valid one, without a password.

        The old implementation called ``authenticate(username, "")``, and
        ``authenticate`` does not skip the password check, so this returned
        None for every token it was given. Minting now lives in ``_mint_token``
        and there is no password path here at all.
        """
        validated = self._validated(token)
        if validated is None:
            return None
        context, payload = validated
        if not context.user:
            return None

        # Re-read the user rather than trusting the token's copy: roles,
        # permissions and is_active may all have changed since it was minted,
        # and re-signing a stale snapshot silently extends revoked access.
        user_data = self._users.get(self._key(context.user.username))
        if not user_data:
            logger.warning(f"Refresh failed: user {context.user.username} no longer exists")
            return None

        user = user_data["user"]
        if not user.is_active:
            logger.warning(f"Refresh failed: user {user.username} is inactive")
            return None

        # The lifetime cap is measured from the original issue time. Every token
        # this service mints carries one; a token without it was signed
        # elsewhere, and refreshing it would leave the cap nothing to measure.
        original_iat = payload.get("oiat")
        if original_iat is None:
            logger.warning(f"Refresh failed: the token for {user.username} has no oiat claim")
            return None

        if self.config.refresh_max_lifetime_hours is not None:
            deadline = original_iat + self.config.refresh_max_lifetime_hours * 3600
            if datetime.now(UTC).timestamp() >= deadline:
                logger.warning(
                    f"Refresh failed: {user.username} exceeded the "
                    f"{self.config.refresh_max_lifetime_hours}h maximum refresh lifetime"
                )
                return None

        logger.info(f"Refreshed token for {user.username}")
        return self._mint_token(
            user, original_iat=original_iat, chain_id=payload.get("cid", payload["jti"])
        )

    def revoke_token(self, token: str) -> bool:
        """Revoke a token, and with it every token of its chain.

        A refresh mints a new jti, so a token's jti alone would leave the tokens refreshed
        from it valid. The whole chain is revoked: the tokens refreshed from this one, the
        ones it was refreshed from, and their siblings. A separate login is a separate chain
        and is untouched.

        Returns True when the token can no longer validate: its jti is in the revoked set
        afterwards (including when it already was), or the token has already expired and is
        refused on its own, so no jti is stored for it. Returns False when nothing was
        revoked: the token does not decode with this service's key, or it carries no jti or no
        usable `exp`. The jti is dropped once the token's `exp` has passed. The chain is kept
        until no token of it can be accepted here, whoever refreshes it (`_chain_hold`): for
        ever when `refresh_max_lifetime_hours` is None, so memory then grows with revocations.
        """
        try:
            payload = self._decode(token, options={"verify_exp": False})
        except jwt.PyJWTError as e:
            logger.warning(f"Token revocation failed: {e}")
            return False
        jti = payload.get("jti")
        if not jti:
            logger.warning("Token revocation failed: missing jti claim in token")
            return False
        exp = payload.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, int | float):
            logger.warning("Token revocation failed: missing or malformed exp claim in token")
            return False
        chain = payload.get("cid", jti)
        if not isinstance(chain, str) or not chain:
            logger.warning("Token revocation failed: the cid claim has the wrong type")
            return False
        now = time.time()
        self._forget_revocations_past_their_expiry(now)
        self._revoked_chains[chain] = self._chain_hold(payload, now)
        leeway = self.config.leeway_seconds
        if exp + leeway <= now:
            logger.info(f"Token already expired, nothing to revoke: jti={jti}")
            return True
        self._revoked_jtis[jti] = float(exp) + leeway
        logger.info(f"Token revoked: jti={jti}")
        return True

    def _forget_revocations_past_their_expiry(self, now: float) -> None:
        """Drop the revocations that can no longer matter: their tokens are refused regardless."""
        for jti in [jti for jti, exp in self._revoked_jtis.items() if exp <= now]:
            del self._revoked_jtis[jti]
        for chain in [chain for chain, until in self._revoked_chains.items() if until <= now]:
            del self._revoked_chains[chain]

    def add_role(self, username: str, role: str) -> None:
        """Add a role to a user. Raises `ValueError` if there is no such user."""
        self._stored(username)["user"].roles.add(role)
        logger.info(f"Added role {role} to user {username}")

    def add_permission(self, username: str, permission: str) -> None:
        """Add a permission to a user. Raises `ValueError` if there is no such user."""
        self._stored(username)["user"].permissions.add(permission)
        logger.info(f"Added permission {permission} to user {username}")


def get_current_context() -> AuthContext | None:
    """Get current auth context"""
    return auth_context_var.get()


def set_current_context(context: AuthContext) -> None:
    """Set current auth context"""
    auth_context_var.set(context)


def clear_current_context() -> None:
    """Clear current auth context"""
    auth_context_var.set(None)


def get_current_user() -> AuthUser | None:
    """Get current authenticated user"""
    context = get_current_context()
    return context.user if context else None


def _refuse_unusable_names(names: tuple[Any, ...], decorator: str, correct: str) -> None:
    """Refuse a role or permission list that could never match, where the mistake is made.

    `@requires_roles()` has nothing to match, so it would decorate cleanly and then refuse every
    caller; a name that is not a string (`@requires_roles(["admin"])`) could not match a role
    either, and a list is not even hashable.
    """
    if not names:
        raise ConfigurationError(
            f"@{decorator} with no names refuses every caller, because the caller must hold one "
            f"of the names and there are none: write {correct}."
        )
    if not all(isinstance(name, str) for name in names):
        raise ConfigurationError(
            f"@{decorator} takes names as separate string arguments: write {correct}, not a "
            f"list or other object."
        )


def requires_auth(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that requires authentication"""

    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        context = get_current_context()
        if not context or not context.is_authenticated:
            raise AuthenticationError("Authentication required")
        return await func(*args, **kwargs)

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        context = get_current_context()
        if not context or not context.is_authenticated:
            raise AuthenticationError("Authentication required")
        return func(*args, **kwargs)

    if inspect.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper


def requires_roles(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that requires the caller to hold ANY ONE of the given roles.

    `@requires_roles("admin", "support")` admits a caller with either role; it does not require
    both. With no roles, or with anything but strings, it raises `ConfigurationError` where it
    is applied.
    """
    # Guard against bare decorator usage without arguments.
    refuse_bare_use(roles[0] if roles else None, "requires_roles", '@requires_roles("admin")')
    _refuse_unusable_names(roles, "requires_roles", '@requires_roles("admin")')

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            context = get_current_context()
            if not context or not context.is_authenticated:
                raise AuthenticationError("Authentication required")

            user_roles = context.user.roles if context.user else set()
            if not any(role in user_roles for role in roles):
                raise AuthorizationError(f"Required roles: {roles}")

            return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            context = get_current_context()
            if not context or not context.is_authenticated:
                raise AuthenticationError("Authentication required")

            user_roles = context.user.roles if context.user else set()
            if not any(role in user_roles for role in roles):
                raise AuthorizationError(f"Required roles: {roles}")

            return func(*args, **kwargs)

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def requires_permissions(*permissions: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that requires the caller to hold ANY ONE of the given permissions.

    `@requires_permissions("orders:read", "orders:write")` admits a caller with either; it does
    not require both. With no permissions, or with anything but strings, it raises
    `ConfigurationError` where it is applied.
    """
    # Guard against bare decorator usage without arguments.
    refuse_bare_use(
        permissions[0] if permissions else None,
        "requires_permissions",
        '@requires_permissions("orders:write")',
    )
    _refuse_unusable_names(
        permissions, "requires_permissions", '@requires_permissions("orders:write")'
    )

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            context = get_current_context()
            if not context or not context.is_authenticated:
                raise AuthenticationError("Authentication required")

            user_perms = context.user.permissions if context.user else set()
            if not any(perm in user_perms for perm in permissions):
                raise AuthorizationError(f"Required permissions: {permissions}")

            return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            context = get_current_context()
            if not context or not context.is_authenticated:
                raise AuthenticationError("Authentication required")

            user_perms = context.user.permissions if context.user else set()
            if not any(perm in user_perms for perm in permissions):
                raise AuthorizationError(f"Required permissions: {permissions}")

            return func(*args, **kwargs)

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


# Middleware for auth integration
class AuthMiddleware:
    """HTTP middleware that authenticates a request from its ``Authorization`` header.

    Shaped for ``app.middleware("http")(AuthMiddleware(auth_service))``: a callable taking
    ``(request, call_next)``. While ``call_next`` runs, the auth context is the one the
    request's bearer token validates to, or none: a missing, malformed, expired or revoked
    token makes an anonymous request, which proceeds and is refused by the decorators if the
    handler needs authentication. Afterwards the context that was set before the request is
    restored, so the middleware never clears one that an outer caller established.

    The header name is matched case-insensitively and so is the ``Bearer`` scheme, as
    ``AuthExtension`` does for the NATS header, because a request type whose headers are a
    plain dict carries whatever spelling the client sent.
    """

    def __init__(self, auth_service: SimpleAuthService):
        self.auth_service = auth_service

    def _context_for(self, request: Any) -> AuthContext | None:
        auth_header = next(
            (
                str(value)
                for key, value in request.headers.items()
                if str(key).lower() == "authorization"
            ),
            "",
        )
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return self.auth_service.validate_token(token.strip())

    async def __call__(self, request: Any, call_next: Callable[..., Any]) -> Any:
        """Run ``call_next`` with the request's auth context, then restore the previous one."""
        reset = auth_context_var.set(self._context_for(request))
        try:
            return await call_next(request)
        finally:
            auth_context_var.reset(reset)
