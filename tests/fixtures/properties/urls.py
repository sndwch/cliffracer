"""NATS server lists with credentials planted in them, and the fragments of each that must not show.

`generate(rng)` builds one comma-separated server list and returns it with the secrets it planted:
users, passwords and tokens in the userinfo, and values of credential query keys. Passwords are made
to sit at the edges of what can be told from a host: digits only (`user:1234` reads as a host and a
port), digits followed by a path, query or fragment, text holding a comma and another scheme, an
unescaped "@", a percent escape or a space, and text holding a whole server (`,nats://h9:4222`)
followed by another comma and scheme. Credential values are planted in a query, in a fragment, in a
fragment after a query, after a second "#", after a second "?", after a pair with no value
(`?flag?`, `?flag&`) and after another pair (`?x=1&`), under its name or a percent-encoded one
(`%74oken`), and a credential value may hold a "?", or in a fragment a "#". A list may end, after a
server with credentials, in a user and password with no host after them, or start with a token that
starts with a comma.

`leaked(text, secrets, url)` is the set of fragments of the secrets that appear in `text` and not in
`url` outside the secrets. A secret is cut at the characters a URL separates on, and each piece of
four or more characters that is not a scheme name is a fragment.
"""

from __future__ import annotations

import random
import re

SCHEMES = ["nats", "tls", "ws", "wss", "nats+tls", "NATS", "Tls", "", ""]
SCHEME_NAMES = {"nats", "tls", "ws", "wss", "nats+tls"}
SPECIALS = [",", "://", ",nats://", ",tls://", ",NATS://", "@", ":", "/", "?", "#", "%2C", "%40",
            " ", ",nats://h9:4222", ",nats://", "&"]  # fmt: skip

_FRAGMENT_SEPARATORS = re.compile(r"://|[,/?#@:%& =]")


def _word(rng: random.Random, length: int) -> str:
    return "".join(
        rng.choice("abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(length)
    )


def _secret(rng: random.Random, secrets: list[str], digits: bool = False) -> str:
    if digits or rng.random() < 0.15:
        text = str(rng.randrange(10**8, 10**9))
    else:
        text = "S" + _word(rng, 7)
    for _ in range(rng.randrange(0, 3)):
        at = rng.randrange(0, len(text) + 1)
        text = text[:at] + rng.choice(SPECIALS) + text[at:]
    if rng.random() < 0.05:  # a whole server inside the password, then another server's start
        text = text + ",nats://h9:4222,nats://" + _word(rng, 4)
    if rng.random() < 0.25:  # digits then a separator: the edge of the stated limit
        number = str(rng.randrange(1000, 99999))
        tail = rng.choice(
            ["/x", "?a=1", "#f", ",nats://" + _word(rng, 3),
             ",tls://" + _word(rng, 3) + ":" + str(rng.randrange(1, 9999))]
        )  # fmt: skip
        text = number + tail + "Q" + _word(rng, 6)
    secrets.append(text)
    return text


def _host(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.5:
        host = _word(rng, 3).lower() + str(rng.randrange(1, 9))
    elif r < 0.7:
        host = f"10.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(256)}"
    elif r < 0.85:
        host = "[::1]"
    else:
        host = _word(rng, 4).lower() + ".example"
    port = rng.random()
    if port < 0.7:
        host += ":" + str(rng.randrange(1, 65535))
    elif port < 0.8:
        host += ":0" + str(rng.randrange(1, 9999))
    elif port < 0.85:
        host += ":" + str(rng.randrange(70000, 99999))
    return host


def _server(rng: random.Random, secrets: list[str]) -> str:
    scheme = rng.choice(SCHEMES)
    prefix = f"{scheme}://" if scheme else ""
    r = rng.random()
    if r < 0.4:
        userinfo = ""
    elif r < 0.5:
        userinfo = _secret(rng, secrets) + "@"  # a token
    elif r < 0.6:
        user = "U" + _word(rng, 6)
        secrets.append(user)
        userinfo = user + "@"
    else:
        user = ("U" + _word(rng, 6)) if rng.random() < 0.6 else _word(rng, 5).lower()
        secrets.append(user)
        userinfo = f"{user}:{_secret(rng, secrets)}@"
    host = _host(rng)
    query = ""
    if rng.random() < 0.2:
        name = rng.choice(["token", "pass", "password", "user", "x", "nkey", "%74oken", "pa%73s"])
        value = "Q" + _word(rng, 7)
        shape = rng.choice(
            [
                "?{}",
                "#{}",
                "?x=1#{}",
                "#x=1#{}",
                "?x=1?{}",
                "?flag?{}",
                "?x=1&{}",
                "#x=1&{}",
                "?flag&{}",
            ]
        )
        if (
            rng.random() < 0.2
        ):  # a value holding a "?", or in a fragment a "#", which is not its end
            mark = rng.choice(["?", "#"]) if "#" in shape else "?"
            value = value[:4] + mark + value[4:]
        if name != "x":
            secrets.append(value)
        query = shape.format(f"{name}={value}")
    return prefix + userinfo + host + query


def generate(rng: random.Random) -> tuple[str, list[str]]:
    """One server list and the secrets planted in it."""
    secrets: list[str] = []
    servers = [_server(rng, secrets) for _ in range(rng.randrange(1, 5))]
    if "@" in servers[-1] and rng.random() < 0.1:  # a user and password after a credentialed host
        user, password = "U" + _word(rng, 6), "S" + _word(rng, 7)
        secrets += [user, password]
        servers.append(f"{user}:{password}")
    separator = rng.choice([",", ",", ",", ", ", ","])
    text = separator.join(servers)
    if rng.random() < 0.01:  # a token that starts the list with a comma, a whole server inside it
        token = ",nats://h9:4222,nats://T" + _word(rng, 6)
        secrets.append(token)
        text = f"{token}@{_host(rng)}{separator}{text}"
    return text, secrets


def fragments(secret: str) -> set[str]:
    return {
        piece
        for piece in _FRAGMENT_SEPARATORS.split(secret)
        if len(piece) >= 4 and piece.lower() not in SCHEME_NAMES
    }


def leaked(text: str, secrets: list[str], url: str) -> set[str]:
    """The fragments of `secrets` that appear in `text`, in any case (a host is printed
    lower-cased), except one that `url` also holds outside every secret: a fragment that is part of
    a host or a port (`9131` of `eww1:91316`) may be printed as that."""
    outside = url
    for secret in sorted(secrets, key=len, reverse=True):
        outside = outside.replace(secret, "\0")
    text, outside = text.lower(), outside.lower()
    return {
        fragment
        for secret in secrets
        for fragment in fragments(secret)
        if fragment.lower() in text and fragment.lower() not in outside
    }
