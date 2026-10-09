"""A per-session broker namespace, and the cleanup that keeps it from piling up.

Two suites that share a broker collide on every global name: stream names,
durable consumers, KV buckets, and the subjects themselves. Each session takes
one prefix and puts it on all of them through ``CLIFFRACER_SUBJECT_PREFIX``,
which ``ServiceConfig.subject_prefix`` defaults from.

**The prefix is per session, not per test.** A leaked consumer or an undrained
stream then belongs to a prefix something will clean up, rather than to one
nothing will ever use again -- which turns cross-talk into an unbounded pile of
dead streams on the broker instead of fixing it. The broker on this host was
carrying 69 streams when that was measured, 44 of them from a single pair of
runs that cleaned up after nothing.
"""

from __future__ import annotations

import os
import time
import uuid

PREFIX_ENV = "CLIFFRACER_SUBJECT_PREFIX"
RUN_ID_ENV = "CLIFFRACER_TEST_RUN_ID"
ENABLE_ENV = "CLIFFRACER_TEST_ISOLATE"

# A prefix names streams and durables as well as subjects, so it is limited to
# what those accept: letters, digits and underscore, no dot.
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


# Spellings of "off". A variable set to one of these turns isolation off; a
# variable set to anything else, or to nothing at all, leaves the default.
FALSEY_SPELLINGS = frozenset({"0", "false", "no", "off"})


def _setting(value: str | None) -> str | None:
    """A variable's value, or None when it says nothing.

    Empty is absence, not a setting. `CLIFFRACER_TEST_RUN_ID=""` is what an
    ordinary CI expression or shell produces when the value is missing, and a
    presence test (`"VAR" in os.environ`) would read it as a request and yield
    an empty prefix -- which `ServiceConfig` then refuses at every service
    construction in the tier.

    Shared by both variables so they cannot read the same string differently.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def isolation_requested() -> bool:
    """Whether this run gets its own broker namespace. On unless it says otherwise.

    This was opt-in while the tier still spelled subjects and stream names by
    hand: turning it on for every run would have made those failures the default
    rather than the thing being fixed. The tier now builds every wire name
    through a helper, and the prefix reaches only tests that ask for a broker,
    so the default inverts -- a run that says nothing is isolated.

    Three rows, and the middle one is inherited rather than chosen:

        unset                            isolated (the default)
        CLIFFRACER_TEST_ISOLATE=""       isolated -- empty says nothing
        CLIFFRACER_TEST_ISOLATE=0        NOT isolated

    THE LAST ROW EXTENDS THE EMPTY-IS-ABSENCE RULE RATHER THAN FOLLOWING IT.
    Read literally, `"0"` is a non-empty string and so a request, which would
    turn isolation ON for someone who wrote the one thing people write to mean
    off. `false`, `no` and `off` are accepted for the same reason, stripped and
    case-insensitively.

    The opt-out is for readings that need the unprefixed names: inspecting what
    a run left behind, or reproducing a report against the subjects a deployment
    actually uses.
    """
    setting = _setting(os.environ.get(ENABLE_ENV))
    return not (setting is not None and setting.lower() in FALSEY_SPELLINGS)


def session_prefix() -> str:
    """One token identifying this pytest session on the broker.

    Two sources, because two kinds of concurrency need different ones. Workers
    inside one session are separated by the xdist id. Two *independent* suites
    on one host -- two CI jobs, or a CI job beside a local run -- share no
    pytest identity at all, so the run part comes from the environment when CI
    sets it and from this process otherwise.
    """
    run = _setting(os.environ.get(RUN_ID_ENV))
    if not run:
        seed = f"{os.getpid()}-{int(time.time())}"
        run = uuid.uuid5(uuid.NAMESPACE_OID, seed).hex[:6]
    worker = os.environ.get("PYTEST_XDIST_WORKER", "m")
    token = f"t{_clean(run)}{_clean(worker)}"
    return token[:12]


def prefixed_subject(subject: str) -> str:
    """A subject as it exists on the broker, under this prefix.

    The subject-side companion to ``prefixed_name``: the prefix is a subject
    token here, joined with a dot, where a stream or durable name joins with an
    underscore. For raw ``nats``/JetStream calls only. A ``StreamSpec`` handed
    to a ``ServiceConfig`` must stay bare -- the config prefixes it through
    ``effective_jetstream_streams``, and prefixing it first applies it twice.
    """
    prefix = os.environ.get(PREFIX_ENV) or None
    return f"{prefix}.{subject}" if prefix else subject


def decided_prefix() -> str | None:
    """The prefix this session will use, or None when it is not isolating.

    THE DECISION ITSELF, so it can be asserted without running a pytest session.
    It used to live inside the session fixture, and a test that called
    `isolation_requested()` instead looked like it pinned the precedence below
    while reading only one of the two variables that decide it -- reverting the
    fixture left that test green.

    Precedence, and the first line is the one that needed deciding:

        an explicit opt-out wins over an exported prefix. Otherwise a stale
        `CLIFFRACER_SUBJECT_PREFIX` in a shell defeats `ISOLATE=0`, and the run
        gets neither the unprefixed names the opt-out advertises nor the
        caller's prefix verbatim, since a module token is appended to it.

        an exported prefix otherwise wins over a generated one, because a
        caller who scoped the run deliberately outranks the default.
    """
    if not isolation_requested():
        return None
    return os.environ.get(PREFIX_ENV) or session_prefix()


def prefixed_name(name: str) -> str:
    """A stream or durable name as it exists on the broker, under this prefix.

    For the raw ``nats.connect`` paths in the tier, which hold no
    ``ServiceConfig`` and so cannot use ``ServiceConfig.prefixed_name``. It
    reads the same environment variable that field defaults from, so the two
    agree by construction. A test that spells the bare name looks for a stream
    the service never created, and the error it gets is ``stream not found``
    from a ``consumer_info`` several assertions later -- not from the line that
    is wrong.
    """
    prefix = os.environ.get(PREFIX_ENV) or None
    return f"{prefix}_{name}" if prefix else name


def module_token(module_name: str) -> str:
    """A short stable token separating one test module from another in a session.

    The session prefix separates runs; it does not separate modules inside a
    run. Several modules claim the same DLQ subject space under different
    stream names, and a subject may be claimed by exactly one stream, so under a
    single prefix the second service to start fails. This token is what makes
    the tier isolated by construction rather than by every author remembering.

    Derived from the module name rather than counted, so it is stable across
    runs, across xdist workers, and under `-k`: a rerun of one module reuses its
    own streams instead of stranding the previous run's.
    """
    digest = uuid.uuid5(uuid.NAMESPACE_OID, module_name).hex[:4]
    return f"m{digest}"


def _clean(text: str) -> str:
    return "".join(c for c in text.lower() if c in _ALPHABET)


def sweep_url() -> str:
    """The broker this session should sweep when it tears its prefix down.

    `$CLIFFRACER_TEST_NATS_URL` when the operator named one, otherwise the
    address the suite actually uses -- the same default every service in the run
    connected to.

    It was the env var ALONE, so a plain local `pytest` swept nothing: the
    variable is set in CI, and CI throws its broker away regardless, so the
    compensating sweep never ran anywhere a leaked stream could survive.
    """
    from tests.conftest import broker_url

    return os.environ.get("CLIFFRACER_TEST_NATS_URL") or broker_url()


async def delete_everything_under(prefix: str, url: str) -> dict[str, int]:
    """Remove every stream and bucket whose name carries *prefix*.

    Deleting a stream removes its consumers with it, so durables need no
    separate pass. KV buckets are streams named ``KV_<bucket>``, which is why
    the match is on the prefix appearing after an optional ``KV_``.
    """
    import nats

    nc = await nats.connect(url, name=f"cleanup-{prefix}")
    js = nc.jetstream()
    removed = {"streams": 0, "failed": 0}
    try:
        # One reader, in `cliffracer.core.jetstream`. A sweep that reads one page
        # cannot delete what it cannot see, and this file had its own copy of
        # the loop with a different termination rule -- two rules for one
        # question is how one of them stays wrong.
        from cliffracer.core.jetstream import all_streams

        for info in await all_streams(js):
            name = info.config.name
            if not _belongs_to(name, prefix):
                continue
            try:
                await js.delete_stream(name)
                removed["streams"] += 1
            except Exception:
                removed["failed"] += 1
    finally:
        await nc.close()
    return removed


def _belongs_to(stream_name: str, prefix: str) -> bool:
    """Whether a stream name was created under this prefix.

    ``KV_`` is stripped first: a bucket ``b`` under prefix ``p`` is the stream
    ``KV_p_b``, so matching the raw name would miss every bucket.
    """
    bare = stream_name[3:] if stream_name.startswith("KV_") else stream_name
    return bare.startswith(f"{prefix}_")
