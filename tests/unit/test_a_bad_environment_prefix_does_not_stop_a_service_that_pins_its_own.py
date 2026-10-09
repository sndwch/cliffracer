"""A bad `CLIFFRACER_SUBJECT_PREFIX` does not stop a service that pins its own `subject_prefix`.

`reserved_rpc_method_names()` builds a `ServiceClient` only to read the names it has, and built it
without a `subject_prefix`. A client given none checks the prefix in the environment, so with a bad
`CLIFFRACER_SUBJECT_PREFIX` every service that declares an RPC method failed to start, and
`describe()` and the client generator failed the same way, whatever prefix the service pinned. That
client never addresses anything, so it pins an empty prefix and reads no environment.
"""

import pytest

from cliffracer import CliffracerService, ServiceClient, ServiceConfig, rpc
from cliffracer.core.typed_rpc import reserved_rpc_method_names
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit

BAD = "a.b"


class Pinned(CliffracerService):
    @rpc
    async def ping(self, text: str) -> str:
        return text


def _config() -> ServiceConfig:
    return ServiceConfig(name="pinned", subject_prefix="good")


@pytest.fixture
def bad_environment_prefix(monkeypatch: pytest.MonkeyPatch):
    """The variable set to a value no subject can carry, and the cached names read afresh."""
    reserved_rpc_method_names.cache_clear()
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", BAD)
    yield
    reserved_rpc_method_names.cache_clear()


def test_the_reserved_names_are_read_whatever_the_environment_prefix_is(bad_environment_prefix):
    names = reserved_rpc_method_names()

    assert {"verify", "close", "service", "namespace", "subject_prefix"} <= names


def test_a_service_that_pins_its_prefix_starts_under_a_bad_environment_prefix(
    bad_environment_prefix,
):
    service = Pinned(_config())

    service.container._discover_for_startup()


def test_describe_of_a_service_that_pins_its_prefix_works_under_a_bad_environment_prefix(
    bad_environment_prefix,
):
    description = describe(Pinned, config=_config())

    assert [method.name for method in description.methods] == ["ping"]


def test_CONTROL_the_reserved_names_are_the_same_under_a_good_and_a_bad_prefix(monkeypatch):
    reserved_rpc_method_names.cache_clear()
    monkeypatch.delenv("CLIFFRACER_SUBJECT_PREFIX", raising=False)
    unset = reserved_rpc_method_names()
    reserved_rpc_method_names.cache_clear()
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", BAD)
    bad = reserved_rpc_method_names()
    reserved_rpc_method_names.cache_clear()

    assert unset == bad


def test_CONTROL_a_client_that_takes_the_environment_prefix_still_refuses_a_bad_one(
    bad_environment_prefix,
):
    with pytest.raises(ValueError, match="CLIFFRACER_SUBJECT_PREFIX"):
        ServiceClient(service="orders", verify=False)
