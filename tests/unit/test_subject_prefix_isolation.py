"""Two environments sharing one broker do not meet, and no broker is needed to say so.

`subject_prefix` is the outermost token on every name a service puts on a
broker. The property that matters is not that a prefix appears -- it is that
**nothing one environment subscribes to matches anything another publishes**,
including the `cross_namespace=True` listener whose whole purpose is to match
across namespaces.

That listener is why the prefix is outside the namespace rather than being one.
`*` matches exactly one token, so a prefix used AS the namespace sits inside the
wildcard and is read straight through it. The isolation test below fails on that
shape, which is the design this file exists to rule out.

The namespace behaviour these build on is covered already and not repeated here:
see test_namespace_calls, test_namespace_subscriptions and
test_cross_namespace_listener.
"""

import pytest

from cliffracer import ServiceConfig
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit

PATTERN = "orders.created"
METHOD = "process"


def matches(pattern: str, subject: str) -> bool:
    """NATS subject matching: `*` spans one token, `>` spans the rest."""
    p, s = pattern.split("."), subject.split(".")
    for i, token in enumerate(p):
        if token == ">":
            return i < len(s)
        if i >= len(s):
            return False
        if token != "*" and token != s[i]:
            return False
    return len(p) == len(s)


def _config(**overrides) -> ServiceConfig:
    # subject_prefix is stated explicitly, never inherited from the environment:
    # the session fixture sets CLIFFRACER_SUBJECT_PREFIX for every run, and a
    # test asserting unprefixed behaviour must say so rather than depend on an
    # ambient value it does not control.
    overrides.setdefault("subject_prefix", None)
    return ServiceConfig(
        name="orders_svc",
        health_port=0,
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.>"])],
        **overrides,
    )


def published_subjects(config: ServiceConfig) -> list[str]:
    """Every subject a service on this config puts on the wire."""
    return [
        HandlerDiscovery.with_namespace(config, f"{config.name}.rpc.{METHOD}"),
        HandlerDiscovery.with_namespace(config, PATTERN),
    ]


def subscribed_patterns(config: ServiceConfig) -> list[str]:
    """Every pattern a service on this config subscribes to."""
    return [
        HandlerDiscovery.with_namespace(config, f"{config.name}.rpc.{METHOD}"),
        HandlerDiscovery.effective_event_subject(config, PATTERN, False),
        HandlerDiscovery.effective_event_subject(config, PATTERN, True),
    ]


# --- the matcher is an instrument, so it is checked before it is trusted -------


@pytest.mark.parametrize(
    ("pattern", "subject", "expected"),
    [
        ("a.b", "a.b", True),
        ("a.b", "a.c", False),
        ("*.b", "a.b", True),
        ("*.b", "a.x.b", False),
        ("a.>", "a.b.c", True),
        ("a.>", "a", False),
        ("w7.*.orders", "w7.appA.orders", True),
        ("w7.*.orders", "w8.appA.orders", False),
    ],
)
def test_CONTROL_the_subject_matcher_agrees_with_nats_rules(pattern, subject, expected):
    """`*` is one token and `>` is the tail, or the isolation claim reads nothing."""
    assert matches(pattern, subject) is expected


# --- the prefix is outermost on every shape -----------------------------------


def test_the_prefix_is_outermost_on_a_plain_subject():
    assert HandlerDiscovery.with_namespace(_config(subject_prefix="w7"), PATTERN) == f"w7.{PATTERN}"


def test_the_prefix_is_outside_the_namespace():
    config = _config(namespace="appA", subject_prefix="w7")
    assert HandlerDiscovery.with_namespace(config, PATTERN) == f"w7.appA.{PATTERN}"


def test_a_cross_namespace_listener_wildcards_the_namespace_inside_the_prefix():
    """The wildcard spans the namespace; the prefix stays outside it."""
    config = _config(namespace="appA", subject_prefix="w7")

    assert HandlerDiscovery.effective_event_subject(config, PATTERN, True) == f"w7.*.{PATTERN}"


def test_the_prefix_renders_name_safe_on_streams_and_durables():
    """A stream name and a durable take no dot, so the same prefix renders with an underscore."""
    config = _config(subject_prefix="w7")

    assert config.prefixed_name("ORDERS") == "w7_ORDERS"
    assert config.effective_jetstream_streams[0].name == "w7_ORDERS"
    assert config.effective_jetstream_streams[0].subjects == ["w7.orders.>"]

    # The declaration stays what the author wrote; the prefix is how it is read.
    assert config.jetstream_streams[0].name == "ORDERS"
    assert config.jetstream_streams[0].subjects == ["orders.>"]


def test_a_declared_stream_covers_what_the_service_publishes():
    """The two halves agree, or every JetStream publish is refused."""
    config = _config(subject_prefix="w7")
    claimed = config.effective_jetstream_streams[0].subjects[0]

    for subject in published_subjects(config):
        if subject.startswith("w7.orders."):
            assert matches(claimed, subject), f"{claimed} does not cover {subject}"


def test_a_round_trip_does_not_prefix_twice():
    """Dumping and revalidating a config leaves the same effective names.

    This is why the prefix is a view. Rewriting the declarations at construction
    double-applies here -- the dump carries the prefixed names, and validating it
    prefixes them again -- and `validate_assignment=True` means any later
    assignment to any field revalidates the model as well.
    """
    config = _config(subject_prefix="w7")

    again = ServiceConfig.model_validate(config.model_dump())

    assert again.effective_jetstream_streams[0].name == "w7_ORDERS"
    assert again.effective_jetstream_streams[0].subjects == ["w7.orders.>"]

    assigned = _config(subject_prefix="w7")
    assigned.nats_url = "nats://elsewhere:4222"
    assert assigned.effective_jetstream_streams[0].name == "w7_ORDERS"


# --- the property this exists for ---------------------------------------------


def test_two_environments_share_no_subject():
    """No pattern either environment subscribes to matches anything the other publishes.

    This is the claim. It fails on the design where the prefix IS the namespace,
    because the cross-namespace listener's `*` then spans the prefix and reads
    the other environment straight through it.
    """
    left, right = _config(subject_prefix="w7"), _config(subject_prefix="w8")

    for pattern in subscribed_patterns(left):
        for subject in published_subjects(right):
            assert not matches(pattern, subject), (
                f"{left.subject_prefix!r} subscribes {pattern!r}, which matches "
                f"{subject!r} published by {right.subject_prefix!r}"
            )


def test_CONTROL_using_the_namespace_as_the_prefix_does_not_isolate():
    """The rejected design, shown failing rather than argued against.

    With the environment token as the namespace it sits inside the wildcard, so
    a cross-namespace listener in one environment matches the other's traffic.
    """
    left, right = _config(namespace="w7"), _config(namespace="w8")

    cross = HandlerDiscovery.effective_event_subject(left, PATTERN, True)
    leaked = [s for s in published_subjects(right) if matches(cross, s)]

    assert leaked, (
        "the rejected shape isolated after all, so this control proves nothing "
        "and the design argument needs rechecking"
    )


# --- the field is opt-in ------------------------------------------------------


def test_a_config_without_a_prefix_is_byte_identical_to_today():
    """Every subject shape is unchanged when no prefix is set."""
    plain = _config()
    namespaced = _config(namespace="appA")

    assert HandlerDiscovery.with_namespace(plain, PATTERN) == PATTERN
    assert HandlerDiscovery.with_namespace(namespaced, PATTERN) == f"appA.{PATTERN}"
    assert HandlerDiscovery.effective_event_subject(plain, PATTERN, True) == f"*.{PATTERN}"
    assert plain.prefixed_name("ORDERS") == "ORDERS"
    assert plain.effective_jetstream_streams[0].name == "ORDERS"
    assert plain.effective_jetstream_streams[0].subjects == ["orders.>"]


@pytest.mark.parametrize("bad", ["has.dot", "has star*", "has>gt", "", "has space"])
def test_a_prefix_that_cannot_name_a_stream_is_refused(bad):
    """The prefix names streams and durables too, so it takes no dot or wildcard."""
    with pytest.raises(ValueError, match="subject_prefix"):
        _config(subject_prefix=bad)


# --- set-but-empty is the row that discriminates ------------------------------


@pytest.mark.parametrize(
    ("value", "uses_the_value"),
    [(None, False), ("", False), ("   ", False), ("ci42", True)],
    ids=["unset", "set-but-empty", "whitespace", "set"],
)
def test_an_empty_run_id_reads_as_unset(monkeypatch, value, uses_the_value):
    """A run id set to the empty string names nothing, as an unset one does.

    The WHITESPACE row is what `_setting` rescues, and saying "the middle rows"
    overstated it: `if not run:` already caught the empty string before this,
    so the empty row behaves the same either way. `"   "` is what a presence
    test or a bare `os.environ.get` treats as a name, putting whitespace where
    the run component belongs; treating it as absence falls back to a generated
    token.

    An empty run component is not harmless. It would leave every session in a
    run sharing the prefix `tm`, so two suites would collide on exactly the
    names this exists to separate.

    This used to assert that the run id gated isolation. It no longer gates
    anything -- isolation is on by default -- so the property is pinned where it
    now lives, in the prefix the session builds.
    """
    from tests.broker_isolation import RUN_ID_ENV, session_prefix

    monkeypatch.delenv(RUN_ID_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(RUN_ID_ENV, value)

    prefix = session_prefix()
    assert prefix.startswith("t"), prefix
    # The run component itself, not its length. `len(prefix) > 1` passed on the
    # exact collapse this guards: `"tm"` is two characters, so the marker and
    # the worker id alone satisfied it and the test could not fail for the
    # reason it gives. `"tm"[1:-1]` is empty; `"tde1362m"[1:-1]` is `"de1362"`.
    assert prefix[1:-1], f"the run component collapsed: {prefix!r}"
    if uses_the_value:
        assert value.strip() in prefix, f"{prefix!r} does not carry {value!r}"
    else:
        assert "ci42" not in prefix


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("w7", "w7")],
    ids=["unset", "set-but-empty", "set"],
)
def test_an_empty_prefix_variable_leaves_the_config_unprefixed(monkeypatch, value, expected):
    """The same three rows where the variable reaches `ServiceConfig` directly."""
    monkeypatch.delenv("CLIFFRACER_SUBJECT_PREFIX", raising=False)
    if value is not None:
        monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", value)

    assert ServiceConfig(name="s", health_port=0).subject_prefix == expected


def test_CONTROL_an_empty_namespace_is_refused_outright():
    """Why the row above matters: an empty token is not an inert one."""
    with pytest.raises(ValueError):
        ServiceConfig(name="s", health_port=0, namespace="")


# --- the default, and the one spelling people write to mean off ---------------


@pytest.mark.parametrize(
    ("value", "isolated"),
    [
        (None, True),
        ("", True),
        ("   ", True),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        (" Off ", False),
        ("no", False),
        ("1", True),
        ("yes", True),
        ("true", True),
    ],
    ids=[
        "unset",
        "set-but-empty",
        "whitespace",
        "zero",
        "false",
        "FALSE",
        "padded-Off",
        "no",
        "one",
        "yes",
        "true",
    ],
)
def test_isolation_is_on_unless_the_variable_spells_off(monkeypatch, value, isolated):
    """On by default; an explicitly falsey spelling turns it off.

    `0` is the row that matters, and it EXTENDS the empty-is-absence rule above
    rather than following it. Read literally, `"0"` is a non-empty string and so
    a request, which would turn isolation ON for someone who wrote the one thing
    people write to mean off. `false`, `no` and `off` are accepted for the same
    reason, stripped and case-insensitively.

    Set-but-empty keeps its old meaning exactly: it says nothing, so the default
    applies -- which is now on rather than off.
    """
    from tests.broker_isolation import ENABLE_ENV, isolation_requested

    monkeypatch.delenv(ENABLE_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(ENABLE_ENV, value)

    assert isolation_requested() is isolated


def test_an_explicit_opt_out_outranks_an_exported_prefix(monkeypatch):
    """`ISOLATE=0` wins over `CLIFFRACER_SUBJECT_PREFIX`, and that is a choice.

    Asserted through `decided_prefix`, which is where the precedence lives. The
    first version of this test called `isolation_requested()`, which reads only
    `CLIFFRACER_TEST_ISOLATE` -- so it returned False whatever the prefix was,
    and the test passed with the precedence reverted. It pinned nothing.

    A stale `CLIFFRACER_SUBJECT_PREFIX` left in a shell is exactly how someone
    meets this combination.
    """
    from tests.broker_isolation import ENABLE_ENV, PREFIX_ENV, decided_prefix

    monkeypatch.setenv(PREFIX_ENV, "callerset")

    monkeypatch.setenv(ENABLE_ENV, "0")
    assert decided_prefix() is None, (
        "an exported prefix defeated an explicit opt-out, so the run gets "
        "neither the unprefixed names nor the caller's prefix verbatim"
    )

    monkeypatch.delenv(ENABLE_ENV, raising=False)
    assert decided_prefix() == "callerset", (
        "without the opt-out the caller's exported prefix must still be honoured"
    )


def test_a_generated_prefix_is_used_when_nothing_is_exported(monkeypatch):
    """The third row of the same decision, so the test above cannot pass by
    always returning None."""
    from tests.broker_isolation import ENABLE_ENV, PREFIX_ENV, decided_prefix

    monkeypatch.delenv(PREFIX_ENV, raising=False)
    monkeypatch.delenv(ENABLE_ENV, raising=False)

    prefix = decided_prefix()
    assert prefix, "isolation is on by default, so a prefix must be decided"
    assert prefix.startswith("t"), prefix
    assert prefix != "callerset"
