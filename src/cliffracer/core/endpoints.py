"""What a broker URL and a listener host must look like to be usable at all.

These read a value the way the code that uses it does, and refuse only what that code cannot use
or would quietly use as something else. They check the syntax and never resolve a name or dial
anything, so a host that is merely down or unknown still passes: that is for the connection to
report. Each returns the problem as text, or `None` for a usable value. The text never repeats the
value, because a broker URL may carry a password.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import unquote, urlparse

from .credentials import is_credential_name

#: The schemes nats-py connects with. It reads them in lower case only.
NATS_URL_SCHEMES = ("nats", "tls", "ws", "wss")

_NATS_SCHEME_RE = re.compile(r"^(nats|tls|ws|wss)://", re.IGNORECASE)

#: What urlparse drops from the front of a URL before it reads the scheme.
_URLPARSE_SKIPS = "".join(chr(c) for c in range(0x21))

_SCHEME_SYNTAX = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")

_LABEL_FORBIDDEN = frozenset("/:@?#[]\\,\"'<>|;=+*!$%&()^`~{}")


def unusable_host(host: str) -> str | None:
    """Why `host` cannot be a hostname or an IP address, or `None` when it can.

    An IPv4 or IPv6 literal passes, written without brackets. A name passes when each dot-separated
    label is non-empty, at most 63 characters, free of whitespace and of the characters a URL or a
    list would use, and neither begins nor ends with a hyphen. Underscores are allowed, because
    container and service names use them and resolvers accept them.
    """
    if not host:
        return "it is empty"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    if any(c.isspace() or ord(c) < 32 for c in host):
        return "it contains whitespace"
    if host.startswith("[") or host.endswith("]"):
        return "an IPv6 address is written without brackets"
    name = host[:-1] if host.endswith(".") else host
    if len(name) > 253:
        return "it is longer than 253 characters"
    labels = name.split(".")
    for label in labels:
        if not label:
            return "it has an empty label (a leading or doubled '.')"
        if len(label) > 63:
            return "a label is longer than 63 characters"
        if label.startswith("-") or label.endswith("-"):
            return "a label begins or ends with '-'"
        if bad := sorted(set(label) & _LABEL_FORBIDDEN):
            return f"it contains {''.join(bad)!r}, which a host name cannot"
    if len(labels) == 4 and all(label.isdecimal() for label in labels):
        return "it is written like an IPv4 address but is not one"
    return None


def unusable_nats_url(url: str) -> str | None:
    """Why nats-py cannot connect to `url`, or `None` when it can.

    A string is one server. nats-py reads `nats://`, `tls://`, `ws://` and `wss://`, and a value
    with no scheme as `host` or `host:port` on the default `nats://` scheme. Anything with another
    scheme it takes for a host name, so `http://broker` would dial a host called `http` and wait
    out `connect_timeout` before reporting that nothing answered.
    """
    if not url or not url.strip():
        return "it is empty"
    # Whitespace is not refused as such: nats-py connects with a space or a newline at the front
    # or a newline at the end, which urlparse drops. What it cannot use fails the port or host
    # check below.
    scheme, sep, rest = url.lstrip(_URLPARSE_SKIPS).partition("://")
    if sep:
        if scheme not in NATS_URL_SCHEMES:
            allowed = f"({', '.join(NATS_URL_SCHEMES)}, in lower case)"
            # Only a text shaped like a scheme is repeated. What is in front of a `://` that is
            # not one may be a user and a password.
            if _SCHEME_SYNTAX.fullmatch(scheme):
                return f"the scheme {scheme!r} is not one nats-py connects with {allowed}"
            return f"the text before '://' is not a URL scheme {allowed}"
        if "://" in rest:
            return "it contains a second '://' (one server per URL, no list)"
        text = url
    else:
        text = f"nats://{url}"
    try:
        parsed = urlparse(text)
        port = parsed.port
    except ValueError:
        return "its port is not a number from 1 to 65535"
    if port is not None and port < 1:
        return "its port is not a number from 1 to 65535"
    if "@" in f"{parsed.path}{parsed.query}{parsed.fragment}":
        return (
            "a '/', '?' or '#' in the user or password ends the host early; "
            "percent-encode it (%2F, %3F, %23)"
        )
    host = parsed.hostname
    if host is None or host == "none":
        return "it has no host"
    if (problem := unusable_host(host)) is not None:
        return f"its host is unusable: {problem}"
    return None


#: The schemes `redact_nats_url` keeps in what it prints: the ones nats-py connects with, and the
#: `nats+tls` that tooling writes for a TLS server. Any other text in front of a `://` may be a
#: credential and is redacted with the rest.
_REDACTED_URL_SCHEME_RE = re.compile(r"^(nats\+tls|nats|tls|ws|wss)://", re.IGNORECASE)

#: A comma that starts another server of a list: it is followed by a scheme, after any whitespace
#: (`a, nats://b` is two servers). A comma in a password is not, so a password holding one is not
#: taken for a second server.
_SERVER_LIST_SEPARATOR_RE = re.compile(r",\s*(?=(?:nats\+tls|nats|tls|ws|wss)://)", re.IGNORECASE)

#: A server written without credentials, and with nothing after its port: a scheme, a host (a name,
#: an address or a bracketed IPv6 address) and a numeric port. The user and password of a server
#: are not written this way unless the password is a number. A path, query or fragment may run on
#: into what a comma follows, so a piece that has one is not read as a server of its own.
_SERVER_WITHOUT_CREDENTIALS_RE = re.compile(
    r"(?:nats\+tls|nats|tls|ws|wss)://(?:\[[0-9a-f:.]+\]|[a-z0-9._-]+):\d+",
    re.IGNORECASE,
)

#: Query parameters a broker URL carries a credential in, beyond the names `is_credential_name`
#: already reads as one (`token`, `password`, `secret`, ...). Exact names, not fragments: `pass`
#: alone would match every `bypass` and `passenger`.
_URL_CREDENTIAL_PARAMETERS = (
    "pass",
    "pwd",
    "user",
    "username",
    "user_name",
    "auth",
    "key",
    "nkey",
    "creds",
)


#: Where a pair of a query or a fragment ends: a credential's value only at "&", any other at "&",
#: "?" or "#".
_PAIR_END = re.compile(r"[&?#]")
_PAIR_NAME_END = re.compile(r"[=&?#]")


def _redact_pairs(pairs: str) -> str:
    """`name=value` pairs, each value withheld whose name says it is a credential.

    A credential's value runs to the next "&", whatever "?" or "#" it holds, so no part of it is
    printed. Any other pair's value ends at the next "&", "?" or "#", and another pair starts there,
    so a credential written after one (`a=1?password=x`) is withheld too.
    """
    kept: list[str] = []
    start = 0
    # Each step moves `start` past at least one character, so the loop returns within
    # `len(pairs) + 1` steps for any input. The bound is defence: a change that stopped it moving
    # on would otherwise loop without end, growing `kept`, instead of failing by name.
    for _ in range(len(pairs) + 1):
        name_end = _PAIR_NAME_END.search(pairs, start)
        if name_end and name_end.group() == "=":
            name = pairs[start : name_end.start()]
            if is_credential_name(unquote(name), _URL_CREDENTIAL_PARAMETERS):
                end = pairs.find("&", name_end.end())
                kept.append(f"{name}=***")
            else:
                found = _PAIR_END.search(pairs, name_end.end())
                end = found.start() if found else -1
                kept.append(pairs[start:end] if found else pairs[start:])
        else:
            end = name_end.start() if name_end else -1
            kept.append(pairs[start:end] if name_end else pairs[start:])
        if end < 0:
            return "".join(kept)
        kept.append(pairs[end])
        start = end + 1
    raise RuntimeError(
        f"_redact_pairs did not reach the end of {len(pairs)} characters in {len(pairs) + 1} "
        "steps: its loop no longer moves past a character on each step"
    )


def _redact_query_and_fragment(rest: str) -> str:
    """`rest` with each credential withheld from its query and its fragment. The fragment starts at
    the first "#", so a "?" after it is part of the fragment, not the start of a query."""
    before, hash_sign, fragment = rest.partition("#")
    head, question, query = before.partition("?")
    redacted = f"{head}?{_redact_pairs(query)}" if question else head
    return redacted + (f"#{_redact_pairs(fragment)}" if hash_sign else "")


def _servers_of(text: str) -> list[str]:
    """The servers a comma-separated list names, as far as it can be read without a password's help.

    A comma followed by a scheme starts another server only when that cannot be a password. A piece
    with no "@" that is followed by a piece that has one may be the user and password of that
    server (`nats://u:a,nats://x@h` is one server whose password is `a,nats://x`). It is read as a
    server of its own when it is exactly a scheme, a host and a numeric port (`nats://h1:4222`,
    `tls://[::1]:4222`). A piece that is not (`nats://u:a`, whose port is `a`, `nats://h1`, which
    names no port, or `nats://h1:4222/p`, whose path may run on into a password) is joined to what
    follows, and the whole is redacted up to its last "@", as it is for a single URL, so a password
    holding a comma and a scheme is withheld whole. Once a piece is joined, every piece up to the
    next "@" is joined with it, whatever it looks like. Text after a comma behind the host of a server
    with credentials (`nats://a@h1,u:pw`) is the next server's user and password, written without a
    scheme; when a later piece has an "@", it is joined the same way, so a password holding a comma
    and a scheme is withheld whole there too, and the server in front keeps its host.

    Limits follow. A user and a password that are written as a host and a port (`u:1234`), when the
    password holds a comma and a scheme, are read as a server, and that piece is printed, as is a
    whole server written after it in the password (`u:1234,nats://h9:4222,nats://u2@h`). A
    password holding an unescaped "@" that is followed by a comma and a scheme
    (`ws://u:pw@host,nats://u2@h`) has the shape of two servers, the first one's user and password
    ending at that "@", so the text between the "@" and the comma is printed as its host, and a
    whole server written after it in the password (`pw@host,nats://h9:4222,nats://u2@h`) is printed
    as a server; with the "@" percent-encoded (`%40`) the password is withheld whole. A server
    written with a path, query or fragment in front of a credentialed one is withheld with it. A
    list whose servers carry no scheme (`h1:4222,u:p@h2:4222`) is not split, since a comma alone
    cannot be told from one in a password, so it is redacted as one server up to its last "@", and
    only the last host is printed.
    """
    pieces = _SERVER_LIST_SEPARATOR_RE.split(text)
    servers: list[str] = []
    # None when no join is open: an empty piece (a list that starts with a comma) can open one.
    joined: str | None = None
    for index, piece in enumerate(pieces):
        starts_a_server = joined is None
        joined = piece if joined is None else f"{joined},{piece}"
        an_at_follows = any("@" in later for later in pieces[index + 1 :])
        if (
            "@" not in piece
            and not (starts_a_server and _SERVER_WITHOUT_CREDENTIALS_RE.fullmatch(piece))
            and an_at_follows
        ):
            continue
        userinfo, at, host = joined.rpartition("@")
        host, comma, run_on = host.partition(",")
        if at and comma and an_at_follows:
            servers.append(f"{userinfo}@{host}")
            joined = run_on.lstrip()
            continue
        servers.append(joined)
        joined = None
    return servers


def _redact_one_server(text: str) -> str:
    match = _REDACTED_URL_SCHEME_RE.match(text)
    scheme = match.group(1) if match else ""
    rest = text[match.end() :] if match else text
    userinfo_removed = "@" in rest
    if userinfo_removed:
        # Everything up to the last "@" is the user and password, whatever characters it holds:
        # a password may contain ":", "/", "?" or "://".
        rest = rest.rpartition("@")[2]
        # A host holds no comma. One after the "@" is where this server ran on into the next
        # server's user and password (`nats://a@h1:4222,usr:pw, nats://b@h2`), so what follows
        # it is withheld.
        host, comma, _ = rest.partition(",")
        if comma:
            rest = f"{host},***"
    rest = _redact_query_and_fragment(rest)
    if userinfo_removed:
        return f"{scheme}://***@{rest}" if scheme else f"***@{rest}"
    return f"{text[: match.end()]}{rest}" if match else rest


class BrokerUrl(str):
    """A broker URL that prints without its credentials and is the real URL to everything else.

    It is a `str` holding the whole URL, so a dial, a comparison, `str()` and an f-string use it as
    written. Its `repr` is the redacted form, which is what a config's `repr`, a dump printed or
    logged, `__dict__` and a `rich` rendering show, so the password embedded in a URL does not
    reach a log the way a plain string field would carry it.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return repr(redact_nats_url(str(self)))


def redact_nats_url(url: str) -> str:
    """Strip credentials from a NATS URL, or a comma-separated list of them, for diagnostic logging.

    Invariants:
    - Returns the string unchanged if it holds nothing to withhold.
    - Withholds the user and password (everything before the last "@" of a server) and the value of
      each query or fragment parameter whose name says it is a credential (`?token=`, `?pass=`).
    - Keeps every server of a list, each redacted, and keeps its scheme (`nats`, `tls`, `ws`, `wss`,
      `nats+tls`). Text in front of a "://" that is not one of those is withheld with the user.
    - Never raises an exception; returns fallback string on parsing error.
    """
    try:
        text = str(url)
        if "@" not in text and "?" not in text and "#" not in text:
            return text
        return ",".join(_redact_one_server(part) for part in _servers_of(text))
    except Exception:
        return "<unparseable nats url>"
