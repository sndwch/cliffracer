"""NATS subject pattern logic.

Two distinct questions live here. ``subject_matches`` answers "does this
concrete subject match this pattern?", which is what dispatch and stream
coverage need. ``subjects_overlap`` answers "could these two patterns ever
claim the same subject?", which is what stream declaration needs, because
NATS permits a subject to be claimed by exactly one stream.

The second cannot be expressed in terms of the first: both sides may contain
wildcards.
"""


def validate_subject(subject: str, *, wildcards: bool = True) -> str:
    """Validate a permission pattern or a concrete NATS subject."""
    if not isinstance(subject, str) or not subject:
        raise ValueError("a subject must be a nonempty string")
    if any(ord(char) < 33 or ord(char) == 127 or char.isspace() for char in subject):
        raise ValueError(f"subject contains whitespace or a control character: {subject!r}")
    tokens = subject.split(".")
    for index, token in enumerate(tokens):
        if not token:
            raise ValueError(f"subject has an empty token: {subject!r}")
        if "*" in token or ">" in token:
            if not wildcards or token not in {"*", ">"}:
                raise ValueError(f"subject has an invalid wildcard token: {subject!r}")
            if token == ">" and index != len(tokens) - 1:
                raise ValueError(f"subject has a nonterminal wildcard: {subject!r}")
    return subject


def validate_inbox_prefix(prefix: str) -> str:
    """Require a concrete, explicitly scoped inbox prefix."""
    validate_subject(prefix, wildcards=False)
    if prefix == "_INBOX" or prefix.startswith("$"):
        raise ValueError("choose a dedicated inbox prefix, such as '_INBOX.orders'")
    return prefix


def inbox_subject(inbox_prefix: str | None, *tokens: str) -> str:
    """A subject under a connection's inbox prefix (`_INBOX` when it sets none), as a client's own
    inboxes are: a namespace or an environment prefix does not apply to it."""
    return validate_subject(".".join([inbox_prefix or "_INBOX", *tokens]))


def subject_matches(pattern: str, subject: str) -> bool:
    """Check if subject matches pattern (supports wildcards)"""
    pattern_parts = pattern.split(".")
    subject_parts = subject.split(".")

    if pattern_parts[-1] == ">":
        # In NATS, '>' must match 1 or more tokens at the end of the subject.
        # If pattern has K tokens ending in '>', subject must have at least K tokens.
        if len(subject_parts) < len(pattern_parts):
            return False
        for p, s in zip(pattern_parts[:-1], subject_parts, strict=False):
            if p == "*":
                continue
            elif p != s:
                return False
        return True

    if len(pattern_parts) != len(subject_parts):
        return False

    for p, s in zip(pattern_parts, subject_parts, strict=True):
        if p == "*":
            continue
        elif p != s:
            return False

    return True


def subjects_overlap(a: str, b: str) -> bool:
    """True if two subject patterns could ever claim the same concrete subject."""
    a_parts = a.split(".")
    b_parts = b.split(".")

    for i in range(max(len(a_parts), len(b_parts))):
        at = a_parts[i] if i < len(a_parts) else None
        bt = b_parts[i] if i < len(b_parts) else None

        # One pattern ran out of tokens. A ">" on the other side still needs at
        # least one token to consume, so "a.>" does not overlap bare "a".
        if at is None or bt is None:
            return False

        if at == ">" or bt == ">":
            return True
        if at == "*" or bt == "*":
            continue
        if at != bt:
            return False

    return True
