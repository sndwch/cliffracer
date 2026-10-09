"""Which names say that a value is a credential.

Two places publish data to readers the service did not choose: the NATS log stream, which carries
the structured log record, and the dead-letter record, which carries the headers a message arrived
with. Both withhold a value by the name it sits under, and both ask this module, so a name is added
in one place and the two cannot disagree about it.

The rule is by name. A credential carried under a name that matches none of these, by something
that is not an installed extension, is not recognised.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

#: Names that are a credential when they are the whole name. `Cookie` and `Set-Cookie` carry one,
#: and a header is not withheld for merely containing the word.
CREDENTIAL_NAMES = frozenset({"cookie", "set_cookie"})

#: A name containing any of these is a credential. Written in the canonical form of
#: `canonical_name`: lower case, with every run of other characters as one underscore, so
#: `api-key`, `API_KEY` and `api.key` are all `api_key`.
CREDENTIAL_FRAGMENTS = (
    "access_key",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "encryption_key",
    "jwt",
    "passphrase",
    "passwd",
    "password",
    "private_key",
    "secret",
    "session",
    "signing_key",
    "token",
)


def canonical_name(name: object) -> str:
    """A name in the form the rules are written in: case and punctuation do not matter."""
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def is_credential_name(name: object, extra_names: Iterable[str] = ()) -> bool:
    """Whether a value under `name` is a credential and must not be published.

    `extra_names` are the names an installed extension reads a credential from (see
    `credential_names_of`); a name is a credential when it is one of them, however it is spelled.
    """
    canonical = canonical_name(name)
    if canonical in CREDENTIAL_NAMES:
        return True
    if any(fragment in canonical for fragment in CREDENTIAL_FRAGMENTS):
        return True
    return any(canonical == canonical_name(extra) for extra in extra_names)


def credential_names_of(extensions: Iterable[Any] | None) -> frozenset[str]:
    """The names the installed extensions read a credential from: each one's `header`, lower-cased.

    An extension without a string `header` reads none and contributes none.
    """
    return frozenset(
        ext.header.lower()
        for ext in extensions or ()
        if isinstance(getattr(ext, "header", None), str)
    )


def bearer(token: str) -> str:
    """The value of an `Authorization`-style header for a token: `Bearer <token>`, once.

    A token already carrying the scheme, in any case, is returned as it is, so a factory may
    return either `abc` or `Bearer abc` and the header never reads `Bearer Bearer abc`.
    """
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"
