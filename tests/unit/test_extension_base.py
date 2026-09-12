import pytest

from cliffracer.core.extension import (
    Extension,
    ExtensionIsolationError,
    SharedDependency,
    WorkerContext,
    entrypoint,
)

pytestmark = pytest.mark.unit


class Recorder(Extension):
    def __init__(self):
        self.calls: list[str] = []


def test_bind_returns_a_copy_that_knows_its_service_and_name():
    ext = Recorder()
    bound = ext.bind(service="svc", name="rec")
    assert bound is not ext
    assert bound.service == "svc"
    assert bound.name == "rec"
    assert bound._origin is ext
    assert ext.service is None


async def test_default_hooks_are_no_ops_and_details_are_none():
    ext = Recorder().bind(service=None, name="x")
    ctx = WorkerContext(kind="rpc", subject="s", headers={}, correlation_id=None, payload={})
    await ext.setup(None)
    await ext.start()
    await ext.worker_setup(ctx)
    await ext.worker_result(ctx, None, None)
    await ext.worker_teardown(ctx)
    await ext.stop()
    assert ext.health_details() is None
    assert ext.info_details() is None
    assert ext.entrypoint_kinds() == {}


async def test_per_instance_state_created_in_setup_is_not_shared_between_services():
    """Verify mutable state initialized in setup() is isolated between service instances."""

    class Counter(Extension):
        async def setup(self, ctx):
            self.seen: list[str] = []

    a = Counter().bind(service="a", name="c")
    b = a._origin.bind(service="b", name="c")
    await a.setup(None)
    await b.setup(None)
    a.seen.append("only-a")
    assert b.seen == []


def test_state_built_in_init_is_isolated_across_bound_instances():
    """Verify attributes initialized in __init__ are completely isolated per service."""
    origin = Recorder()
    a = origin.bind(service="a", name="r")
    b = origin.bind(service="b", name="r")
    a.calls.append("from-a")
    assert b.calls == []
    assert a.calls is not b.calls
    assert a.calls is not origin.calls


def test_extension_specification_is_immutable():
    """Verify attempting to mutate an extension specification after freeze raises AttributeError."""
    origin = Recorder()
    origin.freeze()
    with pytest.raises(AttributeError, match="Cannot mutate attribute"):
        origin.calls = ["mutated"]


def test_entrypoint_marker_records_kind_spec_and_owner():
    owner = Recorder()

    @entrypoint("thing", owner=owner, path="/x")
    def handler():
        pass

    assert handler._cliffracer_entrypoints == [("thing", {"path": "/x"}, owner)]


def test_arbitrary_object_dependencies_are_deepcopied_per_bound_instance():
    """Verify arbitrary object instances passed to extensions are isolated per service."""

    class ExternalStore:
        def __init__(self, items: list[str] | None = None):
            self.items = items if items is not None else []

    class StoreExtension(Extension):
        def __init__(self, store: ExternalStore):
            self.store = store

    shared_store = ExternalStore(["init"])
    origin = StoreExtension(shared_store)

    a = origin.bind(service="svc_a", name="store_ext")
    b = origin.bind(service="svc_b", name="store_ext")

    assert a.store is not b.store
    assert a.store is not shared_store
    assert b.store is not shared_store

    a.store.items.append("item-a")
    assert b.store.items == ["init"]
    assert a.store.items == ["init", "item-a"]
    assert shared_store.items == ["init"]


def test_callable_factories_are_invoked_per_bound_instance():
    """Verify callable factories passed as extension arguments create fresh instances."""

    class Client:
        def __init__(self, client_id: int):
            self.client_id = client_id

    class ClientExtension(Extension):
        def __init__(self, client_factory):
            self.client = client_factory

    counter = 0

    def factory():
        nonlocal counter
        counter += 1
        return Client(counter)

    origin = ClientExtension(factory)
    a = origin.bind(service="svc_a", name="client_ext")
    b = origin.bind(service="svc_b", name="client_ext")

    assert a.client is not b.client
    assert a.client.client_id == 1
    assert b.client.client_id == 2


def test_uncopyable_dependencies_fallback_safely_without_crash():
    """Verify uncopyable dependencies (e.g. threading.Lock) raise ExtensionIsolationError unless wrapped in SharedDependency."""
    import threading

    class UncopyableDep:
        def __init__(self):
            self.lock = threading.Lock()

    class LockedExtension(Extension):
        def __init__(self, dep: UncopyableDep):
            self.dep = dep

    dep = UncopyableDep()
    origin = LockedExtension(dep)
    with pytest.raises(ExtensionIsolationError):
        origin.bind(service="svc", name="locked")

    shared_origin = LockedExtension(SharedDependency(dep))
    bound = shared_origin.bind(service="svc", name="locked")
    assert bound.dep is dep
