"""
Pytest configuration and fixtures for Cliffracer testing
"""

import asyncio
import os

import pytest
import pytest_asyncio

from cliffracer.core.correlation import correlation_id_var
from cliffracer.testing import MockMessage
from tests.fixtures.secured_broker import secured_broker


@pytest_asyncio.fixture
async def nats_connection():
    """Connect to a real NATS server for integration tests.

    Yields a live nats.aio client connected to the suite's broker and
    closes it on teardown. Tests using this fixture should be marked
    @pytest.mark.nats_required.
    """
    import nats

    nc = await nats.connect(broker_url())
    try:
        yield nc
    finally:
        if not nc.is_closed:
            await nc.drain()


# Test utilities
class TestServiceHelper:
    """Helper class for service testing"""

    @staticmethod
    def create_mock_message(
        subject: str, data: dict = None, reply: str = "_INBOX.test"
    ) -> MockMessage:
        """Create a mock NATS message"""
        import json

        message_data = json.dumps(data or {}).encode()
        return MockMessage(subject, message_data, reply=reply)


@pytest.fixture
def test_helper():
    """Test helper utilities"""
    return TestServiceHelper


# --- Broker machinery re-exports -------------------------------------------
from conftest import (  # noqa: E402
    DEFAULT_BROKER_URL,
    NATS_PROBE_TIMEOUT_S,
    TEST_BROKER_URL_ENV,
    _apply_broker_url,
    _broker_is_listening,
    broker_url,
    configured_broker_url,
    console_script,
)

__all__ = [
    "secured_broker",
    "DEFAULT_BROKER_URL",
    "NATS_PROBE_TIMEOUT_S",
    "TEST_BROKER_URL_ENV",
    "_apply_broker_url",
    "_broker_is_listening",
    "broker_url",
    "configured_broker_url",
    "console_script",
]


def declared(svc):
    """Return user-declared extension names, excluding container built-ins."""
    return [e.name for e in svc._extensions if not e.name.startswith("_")]


@pytest.fixture(autouse=True)
def _reset_correlation_id_var(request):
    """Reset correlation_id_var after every test, and name the test that left it set.

    A leaked id is a failure of the test that left it, reported at its teardown, rather than
    an id the next test inherits or a reset that hides it. A test that leaves it set on
    purpose says so with `@pytest.mark.leaves_correlation_id`.

    It sees only what a test leaves in the context it ran in: an async test runs in a task
    with a copy of the context, so a set inside one is gone when it ends. Whether dispatch
    isolates the id between messages is read by the dispatch tests, not here.
    """
    yield
    leaked = correlation_id_var.get()
    correlation_id_var.set(None)
    if request.node.get_closest_marker("leaves_correlation_id") is None:
        assert leaked is None, f"this test left correlation_id_var set to {leaked!r}"


@pytest.fixture(scope="session", autouse=True)
def _broker_namespace():
    """Give this session its own prefix on the broker, and clean it up after.

    Set before any service is built, because `ServiceConfig.subject_prefix`
    reads the environment at construction. Torn down by deleting every stream
    and bucket the prefix created, so a broker shared with other sessions does
    not accumulate this one's leftovers.

    A session that already has a prefix set keeps it: a caller that scoped the
    run deliberately outranks this.

    IT DOES NOT EXPORT THE PREFIX ITSELF. `ServiceConfig` reads the environment
    at construction, so exporting it for the whole run reached the in-memory
    tiers too, where a test hand-delivers a message built from a bare subject
    literal while the container subscribes under the prefix. Nothing matched,
    no handler ran, and the assertion read `assert [] == [...]` -- a failure
    that says nothing about prefixes. `_module_namespace` sets it, and only for
    a module that asks for a broker.
    """
    from tests.broker_isolation import (
        PREFIX_ENV,
        decided_prefix,
        delete_everything_under,
        sweep_url,
    )

    # The decision lives in `decided_prefix`, not here, so a unit test can assert
    # the precedence directly instead of through a pytest session.
    prefix = decided_prefix()
    if prefix is None:
        # The opt-out has to reach the NAMES, not only this decision. Five
        # readers take the prefix from `$CLIFFRACER_SUBJECT_PREFIX` --
        # `prefixed_name` and `prefixed_subject` here, and
        # `ServiceConfig.subject_prefix`, `ServiceClient._subject` and the
        # generate-client CLI's `describe_subject` in the package -- so an
        # inherited value left in place gives a run that opted out neither the
        # unprefixed names the opt-out advertises nor the caller's prefix
        # verbatim. Clearing it here is what makes one decision produce one
        # answer.
        #
        # The count is load-bearing for the next reader's choice of remedy, not
        # for this code: clearing at the environment level covers every reader
        # there is, while a list of three invites a fix applied reader by reader
        # that leaves two behind. `decided_prefix` is not among them -- it is
        # the decision these five read the answer of.
        #
        # Cleared here rather than by having the helpers call `decided_prefix()`
        # themselves: with no exported prefix that function generates one from
        # `session_prefix()`, whose seed carries `int(time.time())`, so two
        # calls a second apart return DIFFERENT prefixes. A helper consulting
        # the decision per name would prefix two names in one run differently.
        # The decision belongs in one place that publishes its answer; the
        # helpers read the answer.
        inherited = os.environ.pop(PREFIX_ENV, None)
        try:
            yield None
        finally:
            if inherited is not None:
                os.environ[PREFIX_ENV] = inherited
        return

    existing = os.environ.get(PREFIX_ENV)

    yield prefix

    if existing:
        return
    # A run that named no broker used none: the broker tests were held back, so there is nothing
    # of this prefix to delete, and dialling the default address to look would reach a broker
    # nobody chose. Every run that DID use one named it, so the address is the one it named.
    if not configured_broker_url():
        return
    url = sweep_url()
    try:
        asyncio.run(delete_everything_under(prefix, url))
    except Exception as exc:  # pragma: no cover - teardown must not fail a green run
        # Said out loud: a session that isolated itself and could not clean up
        # has left streams on a shared broker, and returning silently is what
        # let that run for a day.
        print(f"broker cleanup for {prefix!r} on {url} did not complete: {exc}")


def _test_talks_to_a_broker(item) -> bool:
    """Whether THIS test is marked `nats_required`.

    Per test, not per module. `tests/unit/test_idempotent_publishing.py` carries
    one broker test among twenty-six; at module granularity the other
    twenty-five were prefixed too, and they hand-deliver bare subjects, so they
    broke exactly the way the session-wide export broke everything.

    The marker is the declaration that a test reaches a real broker, and
    `tests/repo/test_every_broker_test_carries_the_marker.py` already keeps it
    honest -- so this reads a property the suite maintains rather than a second
    list that can drift from it. Deliberately not the directory: broker-backed
    tests live outside `tests/integration` too, and scoping by tier would leave
    those sharing one unprefixed broker with every other run.
    """
    return item.get_closest_marker("nats_required") is not None


@pytest.fixture(scope="module", autouse=True)
def _module_namespace(request, _broker_namespace):
    """The prefix this module's broker tests use, without exporting it.

    The session prefix separates runs from each other; it does not separate the
    modules inside one run. Several modules declare a DLQ stream over the same
    subject space under different names, and a subject may be claimed by exactly
    one stream, so under a single prefix the second service to start fails.

    Exporting happens per test, in `_isolate_broker_tests` below, because
    `ServiceConfig` reads the variable at construction and a test that never
    touches a broker must not see it.
    """
    if _broker_namespace is None:
        yield None
        return

    from tests.broker_isolation import module_token

    yield f"{_broker_namespace}_{module_token(request.module.__name__)}"


@pytest.fixture(autouse=True)
def _isolate_broker_tests(request, _module_namespace, monkeypatch):
    """Export the prefix for a broker test; make sure no other test sees one.

    Both directions matter, and only one is loud. Setting it too widely breaks
    the in-memory tiers visibly: the container subscribes under the prefix while
    the test hand-delivers a bare subject, nothing matches, and the assertion
    reads `assert [] == [...]`. Leaving it set too narrowly is silent -- the
    broker tests simply stop being isolated and go on passing until they collide
    with another run.

    An inherited value is REMOVED for a non-broker test, not merely left unset:
    a caller can export `CLIFFRACER_SUBJECT_PREFIX` directly, which the session
    fixture honours, and declining to set it would leave that one in place.

    THROUGH `monkeypatch`, NOT BY HAND. `monkeypatch` is one object per test, undone last-in
    first-out, and the root conftest's own autouse fixture asks for it before this one does, so
    it is torn down AFTER this one. A test that then calls `monkeypatch.setenv` on the same
    variable records the value this fixture set, and its undo puts that value back after this
    fixture's teardown had restored the original. A root test never notices, because the next one
    resets the variable; a test outside `tests/`, which has no such fixture, inherits it. Two
    broker tests did exactly that, and the metrics pool and distributed cron tests failed behind
    them. Setting it on the same `monkeypatch` puts both changes on one stack, so they unwind in
    the right order whatever the test does.
    """
    from tests.broker_isolation import PREFIX_ENV

    wanted = _module_namespace if _test_talks_to_a_broker(request.node) else None

    if wanted is None:
        monkeypatch.delenv(PREFIX_ENV, raising=False)
    else:
        monkeypatch.setenv(PREFIX_ENV, wanted)
    yield wanted
