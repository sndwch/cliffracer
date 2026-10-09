"""The NATS log sink publishes on the subject the ingester subscribes to.

The sink built `logs.<service>.<level>` by hand and handed it straight to
`nats_connection.publish`, while the ingester's `@listener("logs.>")` resolved
through `HandlerDiscovery.effective_event_subject` and received the namespace.
The two ends therefore disagreed the moment a namespace existed, and the
failure was silent: the publish succeeded and nothing matched.

BOTH ENDS DERIVE FROM THE SAME SUBJECT BUILDER,
so log streaming is namespaced and later environment-prefixed. An operator who
wants every namespace uses the wildcard form -- `cross_namespace=True` on a
listener, or `*.logs.>` from a shell -- which is a deliberate choice rather
than an accident of two call sites disagreeing.

These assert the agreement, not the string. The published subject is checked to
MATCH the listener's pattern under NATS semantics, using the shipped
`subject_matches`, so the test says the thing that actually matters and cannot
drift by copying a format.

NOT AN END-TO-END RECEIVE, and the reason is worth stating: `ServiceTestHarness`
never opens a socket and does no subject matching, so nothing unit-tier can
observe a real delivery. The agreement assertion below is the load-bearing one.
"""

import asyncio
import re
from pathlib import Path

import pytest
from cliffracer_logging.config import LoggingConfig
from loguru import logger

from cliffracer import ServiceConfig
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.subjects import subject_matches

pytestmark = pytest.mark.unit

# What examples/logging/log_ingester.py declares.
INGESTER_PATTERN = "logs.>"
SERVICE = "user_service"

DEPLOYMENTS = [
    pytest.param(None, None, id="no-namespace"),
    pytest.param("appA", None, id="namespaced"),
    pytest.param("appA", "w7", id="namespaced-and-prefixed"),
]


class RecordingNats:
    """A connection that records the subjects the sink publishes on."""

    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append(subject)


async def _subjects_the_sink_publishes(config: ServiceConfig, message: str) -> list[str]:
    """Every subject the sink used for *message*.

    `add_nats_sink` logs its own "streaming enabled" line through the sink it
    just registered, so the caller's message is selected by text rather than by
    position -- taking `published[0]` would read that line instead.
    """
    nc = RecordingNats()
    sink_id = LoggingConfig.add_nats_sink(
        service_name=SERVICE, nats_connection=nc, config=config, log_level="INFO"
    )
    try:
        logger.bind(service=SERVICE).info(message)
        # loguru's enqueue=True hands off to a thread, and the sink then uses
        # run_coroutine_threadsafe; neither is awaited by the caller.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if nc.published:
                break
    finally:
        logger.remove(sink_id)
    return list(nc.published)


@pytest.mark.asyncio
@pytest.mark.parametrize(("namespace", "prefix"), DEPLOYMENTS)
async def test_the_sink_publishes_where_the_ingester_listens(namespace, prefix):
    """The agreement itself, which is the property the bug broke."""
    config = ServiceConfig(name=SERVICE, namespace=namespace, subject_prefix=prefix, health_port=0)
    published = await _subjects_the_sink_publishes(config, "a line worth streaming")
    assert published, "the sink published nothing; this test can no longer see the subject"

    subscribed = HandlerDiscovery.effective_event_subject(
        config, INGESTER_PATTERN, cross_namespace=False
    )
    unmatched = [s for s in published if not subject_matches(subscribed, s)]
    assert not unmatched, (
        f"the sink published {unmatched} but the ingester subscribes to {subscribed!r}, "
        f"so these logs reach nobody. The publish succeeds and nothing matches, which "
        f"is why this is silent in a running deployment."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("namespace", "prefix"), DEPLOYMENTS)
async def test_the_published_subject_is_the_shared_builders_output(namespace, prefix):
    """And it is that builder's output, not a string that happens to agree.

    Matching alone would also pass for a sink that published the pattern's own
    prefix and nothing else. This pins which helper produced it.
    """
    config = ServiceConfig(name=SERVICE, namespace=namespace, subject_prefix=prefix, health_port=0)
    published = await _subjects_the_sink_publishes(config, "a line worth streaming")

    expected = HandlerDiscovery.with_namespace(config, f"logs.{SERVICE}.info")
    assert published == [expected] * len(published), (
        f"published {published}, expected every entry to be {expected!r}"
    )


@pytest.mark.parametrize(("namespace", "prefix"), DEPLOYMENTS)
def test_CONTROL_the_hand_built_subject_is_what_the_ingester_misses(namespace, prefix):
    """The defect, pinned, so the fix cannot be reverted quietly.

    This is the exact expression the sink used before: an f-string with no
    namespace and no prefix. Under a namespace it does not match, and this
    records that rather than leaving it to the reader to believe.
    """
    config = ServiceConfig(name=SERVICE, namespace=namespace, subject_prefix=prefix, health_port=0)
    hand_built = f"logs.{SERVICE}.info"
    subscribed = HandlerDiscovery.effective_event_subject(
        config, INGESTER_PATTERN, cross_namespace=False
    )

    if namespace is None and prefix is None:
        assert subject_matches(subscribed, hand_built), (
            "with no namespace the old form matched, which is why this shipped"
        )
    else:
        assert not subject_matches(subscribed, hand_built), (
            f"{hand_built!r} now matches {subscribed!r}; if that is deliberate, this "
            f"test and the sink's use of the builder need revisiting together"
        )


@pytest.mark.asyncio
async def test_an_operator_watching_every_namespace_uses_the_wildcard_form():
    """The answer for the case the namespacing decision has to serve.

    Watching every namespace is a reasonable thing to want, and the decision is
    that it is asked for rather than arrived at: a `cross_namespace=True`
    listener reads them all, within its own environment prefix.
    """
    config = ServiceConfig(name=SERVICE, namespace="appA", subject_prefix="w7", health_port=0)
    published = await _subjects_the_sink_publishes(config, "a line worth streaming")

    watcher = ServiceConfig(name="ops", namespace="ops_ns", subject_prefix="w7", health_port=0)
    every_namespace = HandlerDiscovery.effective_event_subject(
        watcher, INGESTER_PATTERN, cross_namespace=True
    )

    assert all(subject_matches(every_namespace, s) for s in published), (
        f"a cross-namespace watcher on {every_namespace!r} did not see {published}"
    )
    own_namespace_only = HandlerDiscovery.effective_event_subject(
        watcher, INGESTER_PATTERN, cross_namespace=False
    )
    assert not any(subject_matches(own_namespace_only, s) for s in published), (
        "the watcher's own-namespace pattern saw another namespace's logs, so "
        "cross_namespace is not what separates them"
    )


README = Path(__file__).resolve().parents[3] / "examples" / "logging" / "README.md"

# The sentence in the README naming a pattern per deployment, and the
# deployments its three patterns are offered for, in order.
README_CLAIM = re.compile(
    r"With\s+neither a prefix nor a namespace that is `([^`]+)`;\s*"
    r"with a namespace, `([^`]+)`;\s*with both, `([^`]+)`",
    re.S,
)
CLAIM_DEPLOYMENTS = [(None, None), ("appA", None), ("appA", "w7")]


def readme_patterns() -> list[str]:
    """The three patterns the README names, read from the file.

    READ RATHER THAN RESTATED. Writing them into this test would pin the
    behaviour and say nothing about whether the document agrees with it -- which
    is the failure this whole file exists because of: a publisher and a reader
    that each made a defensible choice and never compared them. A test that
    restates the README has the same shape as the bug it is guarding.
    """
    found = README_CLAIM.search(README.read_text())
    assert found, (
        f"{README.name} no longer contains the sentence naming a pattern per "
        f"deployment, so this test cannot check the document against the code. "
        f"If the wording moved, move this pattern with it rather than deleting it."
    )
    return list(found.groups())


def test_the_readme_names_a_pattern_for_each_deployment_this_test_knows():
    """A positive reading: the sentence was found and yields three patterns."""
    patterns = readme_patterns()
    assert len(patterns) == len(CLAIM_DEPLOYMENTS), patterns
    assert patterns == sorted(patterns, key=len), (
        f"{README.name}'s patterns are no longer in increasing-scope order: {patterns}"
    )


@pytest.mark.parametrize("index", range(len(CLAIM_DEPLOYMENTS)), ids=lambda i: f"claim-{i}")
def test_the_shell_pattern_the_README_gives_takes_one_star_per_scoping_token(index):
    """`examples/logging/README.md` tells an operator which `nats sub` to run.

    A `*` spans exactly one token, so there is no single pattern covering every
    deployment, and a pattern with too few tokens matches nothing and reports
    nothing about it -- the same silence this whole fix is about. The claim is
    read out of the file, so the document and the code cannot drift apart.
    """
    namespace, prefix = CLAIM_DEPLOYMENTS[index]
    watch_all = readme_patterns()[index]

    config = ServiceConfig(name=SERVICE, namespace=namespace, subject_prefix=prefix, health_port=0)
    published = HandlerDiscovery.with_namespace(config, f"logs.{SERVICE}.info")

    assert subject_matches(watch_all, published), (
        f"{README.name} gives {watch_all!r} for namespace={namespace!r} "
        f"prefix={prefix!r}, which does not match {published!r}"
    )
    too_few = watch_all.replace("*.", "", 1)
    if too_few != watch_all:
        assert not subject_matches(too_few, published), (
            f"{too_few!r} also matches {published!r}, so the star count is not "
            f"what {README.name} claims it is"
        )


def test_the_config_cannot_be_filled_by_a_positional_log_level():
    """`config` is keyword-only, so the pre-namespace call shape is a TypeError.

    It used to bind: `add_nats_sink("svc", nc, "DEBUG")` put the string where
    the config goes, and every log line then failed inside the sink's own
    `except Exception` -- written to stderr, publishing nothing, raising
    nothing. A caller lost their whole log stream and was told about it only in
    a stream nobody reads.
    """
    with pytest.raises(TypeError):
        LoggingConfig.add_nats_sink("svc", RecordingNats(), "DEBUG")  # type: ignore[misc]
