"""Unit tests for Extension factory loud failure and SharedDependency.

Verifies that uncopyable objects raise ExtensionIsolationError during extension
factory instantiation and binding unless explicitly wrapped in SharedDependency.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import CliffracerError
from cliffracer.core.extension import (
    Extension,
    ExtensionIsolationError,
    SharedDependency,
    _safe_clone_arg,
)


class UncopyableObject:
    """An object that explicitly refuses deepcopy."""

    def __deepcopy__(self, memo: Any) -> Any:
        raise TypeError("Cannot deepcopy UncopyableObject")


class StateExtension(Extension):
    """Extension that accepts arbitrary state arguments."""

    def __init__(self, resource: Any, items: list[str] | None = None) -> None:
        self.resource = resource
        self.items = items if items is not None else []


@pytest.mark.unit
def test_extension_isolation_error_hierarchy() -> None:
    """ExtensionIsolationError must inherit from CliffracerError."""
    assert issubclass(ExtensionIsolationError, CliffracerError)
    err = ExtensionIsolationError("test message")
    assert isinstance(err, CliffracerError)


@pytest.mark.unit
def test_safe_clone_arg_uncopyable_lock_raises() -> None:
    """Passing a threading.Lock directly raises ExtensionIsolationError."""
    lock = threading.Lock()
    with pytest.raises(ExtensionIsolationError) as exc_info:
        _safe_clone_arg(lock)

    msg = str(exc_info.value)
    assert "Cannot isolate extension argument" in msg
    assert "lock" in msg or "Lock" in msg
    assert "SharedDependency" in msg


@pytest.mark.unit
def test_safe_clone_arg_uncopyable_custom_object_raises() -> None:
    """Passing custom uncopyable object directly raises ExtensionIsolationError."""
    obj = UncopyableObject()
    with pytest.raises(ExtensionIsolationError) as exc_info:
        _safe_clone_arg(obj)

    msg = str(exc_info.value)
    assert "Cannot isolate extension argument of type 'UncopyableObject'" in msg
    assert "SharedDependency" in msg


@pytest.mark.unit
def test_safe_clone_arg_tuple_with_uncopyable_raises() -> None:
    """Nested tuple with uncopyable item propagates ExtensionIsolationError."""
    nested = (1, "ok", threading.Lock())
    with pytest.raises(ExtensionIsolationError):
        _safe_clone_arg(nested)


@pytest.mark.unit
def test_safe_clone_arg_shared_dependency_unwraps() -> None:
    """Wrapping uncopyable state in SharedDependency returns the identical object."""
    lock = threading.Lock()
    shared = SharedDependency(lock)

    assert shared.value is lock
    assert shared.obj is lock
    assert shared.unwrap() is lock

    result = _safe_clone_arg(shared)
    assert result is lock


@pytest.mark.unit
def test_safe_clone_arg_nested_shared_dependency_in_tuple() -> None:
    """Nested tuple with SharedDependency unwraps correctly."""
    lock = threading.Lock()
    nested = (10, SharedDependency(lock), "hello")

    cloned = _safe_clone_arg(nested)
    assert cloned == (10, lock, "hello")
    assert cloned[1] is lock


@pytest.mark.unit
def test_extension_bind_with_uncopyable_raises_isolation_error() -> None:
    """Binding an extension declaration with uncopyable state fails fast."""
    lock = threading.Lock()
    spec = StateExtension(resource=lock)

    with pytest.raises(ExtensionIsolationError) as exc_info:
        spec.bind(service=None, name="state")

    assert "Cannot isolate extension argument" in str(exc_info.value)


@pytest.mark.unit
def test_extension_bind_with_shared_dependency_succeeds_and_shares() -> None:
    """Binding an extension declaration with SharedDependency succeeds and shares state."""
    lock = threading.Lock()
    shared_lock = SharedDependency(lock)

    class ServiceA(CliffracerService):
        state = StateExtension(resource=shared_lock, items=["initial"])

    class ServiceB(CliffracerService):
        state = StateExtension(resource=shared_lock, items=["initial"])

    svcA = ServiceA(ServiceConfig(name="svc_a", health_port=0))
    svcB = ServiceB(ServiceConfig(name="svc_b", health_port=0))

    # Shared lock is identical by reference
    assert svcA.state.resource is lock
    assert svcB.state.resource is lock
    assert svcA.state.resource is svcB.state.resource

    # Items list was copyable, so it is isolated per instance
    assert svcA.state.items is not svcB.state.items
    svcA.state.items.append("mutation_a")
    assert "mutation_a" not in svcB.state.items


@pytest.mark.unit
def test_extension_create_instance_isolates_copyable_mutables() -> None:
    """Regular lists and dicts are deep-copied independently across instances."""
    items = ["item1", "item2"]
    spec = StateExtension(resource="plain_string", items=items)

    inst1 = spec.create_instance(service=None, name="test1")
    inst2 = spec.create_instance(service=None, name="test2")

    assert inst1.items == ["item1", "item2"]
    assert inst2.items == ["item1", "item2"]
    assert inst1.items is not inst2.items

    inst1.items.append("item3")
    assert inst2.items == ["item1", "item2"]


@pytest.mark.unit
def test_safe_clone_arg_uncopyable_sqlite_connection_raises() -> None:
    """Passing an uncopyable sqlite3.Connection directly raises ExtensionIsolationError."""
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ExtensionIsolationError) as exc_info:
        _safe_clone_arg(conn)

    msg = str(exc_info.value)
    assert "Cannot isolate extension argument" in msg
    assert "Connection" in msg
    assert "SharedDependency" in msg


@pytest.mark.unit
def test_safe_clone_arg_shared_sqlite_connection_unwraps() -> None:
    """Wrapping sqlite3.Connection in SharedDependency unwraps without copying."""
    conn = sqlite3.connect(":memory:")
    shared = SharedDependency(conn)

    assert shared.value is conn
    assert shared.obj is conn
    assert shared.unwrap() is conn

    result = _safe_clone_arg(shared)
    assert result is conn


@pytest.mark.unit
def test_extension_bind_with_uncopyable_sqlite_raises_isolation_error() -> None:
    """Binding an extension with raw sqlite3.Connection raises ExtensionIsolationError."""
    conn = sqlite3.connect(":memory:")

    class SqliteExtension(Extension):
        def __init__(self, db: Any) -> None:
            self.db = db

    class ServiceWithSqlite(CliffracerService):
        db = SqliteExtension(conn)

    with pytest.raises(ExtensionIsolationError) as exc_info:
        ServiceWithSqlite(ServiceConfig(name="svc_sqlite_raw", health_port=0))

    assert "Cannot isolate extension argument" in str(exc_info.value)


@pytest.mark.unit
def test_extension_bind_with_shared_sqlite_shares_identity() -> None:
    """Binding an extension with SharedDependency(sqlite3.Connection) succeeds and shares identity."""
    conn = sqlite3.connect(":memory:")
    shared_conn = SharedDependency(conn)

    class SqliteExtension(Extension):
        def __init__(self, db: Any) -> None:
            self.db = db

    class ServiceWithSharedSqliteA(CliffracerService):
        db = SqliteExtension(shared_conn)

    class ServiceWithSharedSqliteB(CliffracerService):
        db = SqliteExtension(shared_conn)

    svcA = ServiceWithSharedSqliteA(ServiceConfig(name="svc_sqlite_a", health_port=0))
    svcB = ServiceWithSharedSqliteB(ServiceConfig(name="svc_sqlite_b", health_port=0))

    assert svcA.db.db is conn
    assert svcB.db.db is conn
    assert svcA.db.db is svcB.db.db


@pytest.mark.unit
def test_safe_clone_arg_callable_uncopyable_raises() -> None:
    """A callable object requiring arguments that cannot be copied raises ExtensionIsolationError."""

    class CallableUncopyable:
        def __call__(self, arg: Any) -> Any:
            return arg

        def __deepcopy__(self, memo: Any) -> Any:
            raise TypeError("Cannot copy CallableUncopyable")

    obj = CallableUncopyable()
    with pytest.raises(ExtensionIsolationError) as exc_info:
        _safe_clone_arg(obj)

    assert "Cannot isolate extension argument" in str(exc_info.value)


@pytest.mark.unit
def test_safe_clone_arg_callable_zero_arg_factory_produces_isolated_instances() -> None:
    """Zero-argument callable factories generate fresh instances per clone."""

    def factory() -> list[str]:
        return ["isolated_value"]

    inst1 = _safe_clone_arg(factory)
    inst2 = _safe_clone_arg(factory)

    assert inst1 == ["isolated_value"]
    assert inst2 == ["isolated_value"]
    assert inst1 is not inst2


@pytest.mark.unit
def test_safe_clone_arg_standard_function_preserved() -> None:
    """Standard functions (which succeed in copy.deepcopy) are preserved."""

    def compute(val: int) -> int:
        return val * 2

    cloned = _safe_clone_arg(compute)
    assert cloned(5) == 10
