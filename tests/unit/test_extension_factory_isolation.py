"""Isolation and immutability tests for Extension factory pattern."""

from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension, entrypoint

pytestmark = pytest.mark.unit


class CustomState:
    """Non-collection state object to verify reference isolation across bound instances."""

    def __init__(self, value: int = 0) -> None:
        self.value = value


class StatefulExtension(Extension):
    """Extension initializing mutable containers and objects in __init__."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self.mapping: dict[str, int] = {}
        self.tags: set[str] = set()
        self.state: CustomState = CustomState(0)


class ParameterizedExtension(Extension):
    """Extension accepting mutable defaults in declaration arguments."""

    def __init__(
        self,
        items: list[str] | None = None,
        config: dict[str, Any] | None = None,
        tags: set[str] | None = None,
        tuple_with_list: tuple[list[int], ...] | None = None,
    ) -> None:
        self.items = items if items is not None else []
        self.config = config if config is not None else {}
        self.tags = tags if tags is not None else set()
        self.tuple_with_list = tuple_with_list if tuple_with_list is not None else ()


class ChildExtension(Extension):
    """Inner extension used in nested extension tests."""

    def __init__(self, name: str = "child") -> None:
        self.child_name = name
        self.events: list[str] = []


class ParentExtension(Extension):
    """Outer extension containing an inner extension."""

    def __init__(self, child: ChildExtension | None = None) -> None:
        self.child = child or ChildExtension()
        self.parent_records: list[str] = []


def test_mutable_attributes_in_init_are_isolated_across_bound_instances() -> None:
    """Verify mutable attributes created in __init__ are completely isolated."""
    spec = StatefulExtension()
    bound1 = spec.bind("service_1", "stateful")
    bound2 = spec.bind("service_2", "stateful")

    assert isinstance(bound1, StatefulExtension)
    assert isinstance(bound2, StatefulExtension)

    assert bound1 is not bound2
    assert bound1 is not spec
    assert bound2 is not spec

    assert bound1.items is not bound2.items
    assert bound1.mapping is not bound2.mapping
    assert bound1.tags is not bound2.tags
    assert bound1.state is not bound2.state

    assert bound1.items is not spec.items
    assert bound1.mapping is not spec.mapping
    assert bound1.tags is not spec.tags
    assert bound1.state is not spec.state

    bound1.items.append("item_1")
    bound1.mapping["k1"] = 100
    bound1.tags.add("tag_1")
    bound1.state.value = 42

    assert bound2.items == []
    assert bound2.mapping == {}
    assert bound2.tags == set()
    assert bound2.state.value == 0

    assert spec.items == []
    assert spec.mapping == {}
    assert spec.tags == set()
    assert spec.state.value == 0


def test_parameterized_declaration_arguments_are_isolated() -> None:
    """Verify mutable arguments passed at declaration time are cloned per instance."""
    spec = ParameterizedExtension(
        items=["init_item"],
        config={"key": "initial"},
        tags={"init_tag"},
        tuple_with_list=([1, 2],),
    )

    bound1 = spec.bind("service_1", "param")
    bound2 = spec.bind("service_2", "param")

    assert isinstance(bound1, ParameterizedExtension)
    assert isinstance(bound2, ParameterizedExtension)

    assert bound1.items is not bound2.items
    assert bound1.config is not bound2.config
    assert bound1.tags is not bound2.tags
    assert bound1.tuple_with_list[0] is not bound2.tuple_with_list[0]

    bound1.items.append("mutated")
    bound1.config["added"] = True
    bound1.tags.add("mutated_tag")
    bound1.tuple_with_list[0].append(99)

    assert bound2.items == ["init_item"]
    assert bound2.config == {"key": "initial"}
    assert bound2.tags == {"init_tag"}
    assert bound2.tuple_with_list == ([1, 2],)

    assert spec.items == ["init_item"]
    assert spec.config == {"key": "initial"}
    assert spec.tags == {"init_tag"}
    assert spec.tuple_with_list == ([1, 2],)


def test_nested_extensions_are_isolated() -> None:
    """Verify nested extension instances are isolated across bound copies."""
    child_spec = ChildExtension(name="shared_spec_child")
    parent_spec = ParentExtension(child=child_spec)

    bound_parent1 = parent_spec.bind("service_1", "parent")
    bound_parent2 = parent_spec.bind("service_2", "parent")

    assert isinstance(bound_parent1, ParentExtension)
    assert isinstance(bound_parent2, ParentExtension)
    assert isinstance(bound_parent1.child, ChildExtension)
    assert isinstance(bound_parent2.child, ChildExtension)

    assert bound_parent1.child is not bound_parent2.child
    assert bound_parent1.child is not child_spec
    assert bound_parent2.child is not child_spec

    bound_parent1.child.events.append("event_on_1")
    bound_parent1.parent_records.append("parent_rec_1")

    assert bound_parent2.child.events == []
    assert bound_parent2.parent_records == []
    assert child_spec.events == []
    assert parent_spec.parent_records == []


def test_nested_extension_created_in_init_is_isolated() -> None:
    """Verify nested extensions created inside __init__ are reconstructed per bind."""
    parent_spec = ParentExtension()

    bound1 = parent_spec.bind("service_1", "parent")
    bound2 = parent_spec.bind("service_2", "parent")

    assert isinstance(bound1, ParentExtension)
    assert isinstance(bound2, ParentExtension)
    assert isinstance(bound1.child, ChildExtension)
    assert isinstance(bound2.child, ChildExtension)

    assert bound1.child is not bound2.child
    assert bound1.child is not parent_spec.child
    assert bound2.child is not parent_spec.child

    bound1.child.events.append("e1")
    assert bound2.child.events == []
    assert parent_spec.child.events == []


def test_extension_specification_immutability_enforced() -> None:
    """Verify freezing specification locks attribute mutations while bound copies mutate freely."""
    spec = StatefulExtension()
    spec.freeze()

    with pytest.raises(AttributeError, match="Cannot mutate attribute 'items'"):
        spec.items = ["illegal"]

    with pytest.raises(AttributeError, match="Cannot mutate attribute 'mapping'"):
        spec.mapping = {"illegal": 1}

    bound = spec.bind("service_1", "stateful")
    assert isinstance(bound, StatefulExtension)

    bound.items = ["allowed"]
    bound.mapping = {"allowed": 1}

    assert bound.items == ["allowed"]
    assert bound.mapping == {"allowed": 1}


def test_multiple_sequential_binds_produce_independent_instances() -> None:
    """Verify binding the same specification N times yields N distinct isolated instances."""
    spec = StatefulExtension()
    instances = [spec.bind(f"service_{i}", f"ext_{i}") for i in range(10)]

    for i, inst in enumerate(instances):
        assert isinstance(inst, StatefulExtension)
        assert inst._origin is spec
        assert inst.service == f"service_{i}"
        assert inst.name == f"ext_{i}"
        inst.items.append(f"val_{i}")

    for i, inst in enumerate(instances):
        assert isinstance(inst, StatefulExtension)
        assert inst.items == [f"val_{i}"]

    assert spec.items == []


class EntrypointExtension(Extension):
    """Extension tracking entrypoint binding."""

    def __init__(self) -> None:
        self.registered_handlers: list[str] = []

    def entrypoint_kinds(self) -> dict[str, Any]:
        def _binder(
            service: Any, method_name: str, bound_method: Any, spec: dict[str, Any]
        ) -> None:
            self.registered_handlers.append(method_name)

        return {"custom_gate": _binder}


def test_entrypoint_resolution_preserves_spec_owner_identity() -> None:
    """Verify @entrypoint resolves class attribute owner to runtime bound extension."""
    gate_spec = EntrypointExtension()

    class Svc(CliffracerService):
        gate = gate_spec

        @entrypoint("custom_gate", owner=gate_spec)
        async def gated_handler(self) -> str:
            return "ok"

    svc1 = Svc(ServiceConfig(name="svc_1"))
    svc2 = Svc(ServiceConfig(name="svc_2"))

    assert svc1.gate is not svc2.gate
    assert svc1.gate is not gate_spec
    assert svc2.gate is not gate_spec

    assert svc1.gate._origin is gate_spec
    assert svc2.gate._origin is gate_spec

    svc1._discover_handlers()
    svc2._discover_handlers()

    assert isinstance(svc1.gate, EntrypointExtension)
    assert isinstance(svc2.gate, EntrypointExtension)

    assert "gated_handler" in svc1.gate.registered_handlers
    assert "gated_handler" in svc2.gate.registered_handlers
    assert svc1.gate.registered_handlers is not svc2.gate.registered_handlers
