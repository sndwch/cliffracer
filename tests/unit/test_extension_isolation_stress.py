"""Adversarial stress tests for Extension factory binding, state isolation, and freeze immutability."""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit


class DeepNode:
    """Arbitrary custom object nested inside data structures."""

    def __init__(self, value: int = 0) -> None:
        self.value = value
        self.log: list[str] = []
        self.tags: set[str] = set()


class ComplexStressExtension(Extension):
    """Extension featuring multi-layered nested mutable structures.

    `registry` is a declaration ARGUMENT kept as given. Everything else is built
    fresh by `__init__`, so a bound instance gets fresh state from construction
    alone; only an argument the specification captured has to be cloned for an
    instance to be isolated from the others, which is what the stress tests
    below need to exercise.
    """

    def __init__(self, seed: int = 0, registry: dict[str, list[str]] | None = None) -> None:
        self.seed = seed
        self.registry: dict[str, list[str]] = registry if registry is not None else {}
        self.call_log: list[str] = []
        self.stats: dict[str, Any] = {
            "counters": {"read": 0, "write": 0},
            "history": [{"step": 0, "node": DeepNode(seed)}],
        }
        self.tag_registry: set[str] = {f"init_tag_{seed}"}
        self.root_node: DeepNode = DeepNode(seed)


class RecursiveExtension(Extension):
    """Extension supporting arbitrary nesting depth."""

    def __init__(
        self,
        child: RecursiveExtension | None = None,
        depth: int = 0,
        metadata: dict[str, list[int]] | None = None,
    ) -> None:
        self.child: RecursiveExtension | None = child
        self.depth = depth
        self.metadata: dict[str, list[int]] = (
            metadata if metadata is not None else {"vals": [depth]}
        )


class CountingExtension(Extension):
    """Extension carrying per-instance counters, to verify each service binds its own copy."""

    def __init__(self) -> None:
        self.call_counts: dict[str, int] = {}


def test_multithreaded_extension_isolation_stress() -> None:
    """Stress-test extension state isolation across 25 concurrent worker threads."""
    spec = ComplexStressExtension(seed=100, registry={"declared": ["from the declaration"]})
    spec.freeze()

    num_threads = 25
    mutations_per_thread = 200

    def thread_worker(thread_id: int) -> ComplexStressExtension:
        service_id = f"thread_svc_{thread_id}"
        bound = spec.bind(service_id, f"ext_{thread_id}")

        assert isinstance(bound, ComplexStressExtension)
        assert bound._origin is spec
        assert bound.service == service_id

        for step in range(mutations_per_thread):
            # Mutate outer list
            bound.call_log.append(f"{thread_id}:{step}")
            # Mutate nested dictionary
            bound.stats["counters"]["write"] += 1
            # Mutate object inside list inside dictionary
            bound.stats["history"][0]["node"].value += 1
            bound.stats["history"][0]["node"].log.append(f"h_{thread_id}_{step}")
            # Mutate set
            bound.tag_registry.add(f"tag_{thread_id}_{step}")
            # Mutate root object
            bound.root_node.value += 1
            bound.root_node.tags.add(f"root_{thread_id}_{step}")
            # Mutate a declaration argument the specification captured
            bound.registry["declared"].append(f"{thread_id}:{step}")

        return bound

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(thread_worker, i) for i in range(num_threads)]
        bound_instances = [f.result() for f in futures]

    # Verify each instance has strictly its own mutations without cross-instance leakage
    for i, bound in enumerate(bound_instances):
        assert len(bound.call_log) == mutations_per_thread
        assert all(entry.startswith(f"{i}:") for entry in bound.call_log)

        assert bound.stats["counters"]["write"] == mutations_per_thread
        assert bound.stats["counters"]["read"] == 0

        node = bound.stats["history"][0]["node"]
        assert node.value == 100 + mutations_per_thread
        assert len(node.log) == mutations_per_thread
        assert all(entry.startswith(f"h_{i}_") for entry in node.log)

        assert len(bound.tag_registry) == 1 + mutations_per_thread
        assert "init_tag_100" in bound.tag_registry
        for step in range(mutations_per_thread):
            assert f"tag_{i}_{step}" in bound.tag_registry

        assert bound.root_node.value == 100 + mutations_per_thread
        assert len(bound.root_node.tags) == mutations_per_thread

        registry = bound.registry["declared"]
        assert registry[0] == "from the declaration"
        assert len(registry) == 1 + mutations_per_thread
        assert all(entry.startswith(f"{i}:") for entry in registry[1:]), (
            "another instance's mutations reached this instance's declared argument"
        )

    # Verify the frozen specification has 0 mutations
    assert spec.call_log == []
    assert spec.stats["counters"]["write"] == 0
    assert spec.stats["counters"]["read"] == 0
    assert spec.stats["history"][0]["node"].value == 100
    assert spec.stats["history"][0]["node"].log == []
    assert spec.tag_registry == {"init_tag_100"}
    assert spec.root_node.value == 100
    assert spec.root_node.tags == set()
    assert spec.registry == {"declared": ["from the declaration"]}


@pytest.mark.asyncio
async def test_asyncio_concurrent_extension_isolation_stress() -> None:
    """Stress-test extension state isolation across 30 concurrent asyncio tasks with yields."""
    spec = ComplexStressExtension(seed=50, registry={"declared": ["from the declaration"]})
    spec.freeze()

    num_tasks = 30
    cycles = 50

    async def async_worker(task_id: int) -> ComplexStressExtension:
        bound = spec.bind(f"async_svc_{task_id}", f"ext_{task_id}")
        assert isinstance(bound, ComplexStressExtension)

        for c in range(cycles):
            bound.call_log.append(f"task_{task_id}_{c}")
            bound.stats["counters"]["read"] += 1
            bound.root_node.log.append(f"cycle_{c}")
            bound.registry["declared"].append(f"task_{task_id}_{c}")
            await asyncio.sleep(0)  # Yield execution to interleave tasks

        return bound

    tasks = [asyncio.create_task(async_worker(i)) for i in range(num_tasks)]
    bound_instances = await asyncio.gather(*tasks)

    for i, bound in enumerate(bound_instances):
        assert len(bound.call_log) == cycles
        assert all(entry.startswith(f"task_{i}_") for entry in bound.call_log)
        assert bound.stats["counters"]["read"] == cycles
        assert len(bound.root_node.log) == cycles

        registry = bound.registry["declared"]
        assert len(registry) == 1 + cycles
        assert all(entry.startswith(f"task_{i}_") for entry in registry[1:]), (
            "another task's mutations reached this instance's declared argument"
        )

    assert spec.call_log == []
    assert spec.stats["counters"]["read"] == 0
    assert spec.root_node.log == []
    assert spec.registry == {"declared": ["from the declaration"]}


def test_deeply_nested_recursive_extension_isolation() -> None:
    """Verify state isolation across 4 levels of nested extensions."""
    e4 = RecursiveExtension(depth=4, metadata={"vals": [400]})
    e3 = RecursiveExtension(child=e4, depth=3, metadata={"vals": [300]})
    e2 = RecursiveExtension(child=e3, depth=2, metadata={"vals": [200]})
    root = RecursiveExtension(child=e2, depth=1, metadata={"vals": [100]})
    root.freeze()

    b1 = root.bind("svc_1", "root")
    b2 = root.bind("svc_2", "root")

    assert isinstance(b1, RecursiveExtension)
    assert isinstance(b2, RecursiveExtension)
    assert b1 is not b2
    assert isinstance(b1.child, RecursiveExtension)
    assert isinstance(b1.child.child, RecursiveExtension)
    assert isinstance(b1.child.child.child, RecursiveExtension)
    assert isinstance(b2.child, RecursiveExtension)
    assert isinstance(b2.child.child, RecursiveExtension)
    assert isinstance(b2.child.child.child, RecursiveExtension)

    assert b1.child is not b2.child
    assert b1.child.child is not b2.child.child
    assert b1.child.child.child is not b2.child.child.child

    # Mutate deepest node in b1
    b1.child.child.child.metadata["vals"].append(9999)
    # Mutate intermediate node in b1
    b1.child.metadata["vals"].append(8888)

    # Verify b2 is completely unaffected at all levels
    assert b2.child.child.child.metadata["vals"] == [400]
    assert b2.child.child.metadata["vals"] == [300]
    assert b2.child.metadata["vals"] == [200]
    assert b2.metadata["vals"] == [100]

    # Verify root spec and its children specs are completely unaffected
    assert e4.metadata["vals"] == [400]
    assert e3.metadata["vals"] == [300]
    assert e2.metadata["vals"] == [200]
    assert root.metadata["vals"] == [100]


def test_a_declared_extension_is_isolated_across_twenty_plus_concurrent_services() -> None:
    """Verify bound-extension isolation across 25 concurrent CliffracerService instances."""
    gate_spec = CountingExtension()
    gate_spec.freeze()

    class GatedService(CliffracerService):
        gate = gate_spec

    num_services = 25

    def init_service(idx: int) -> GatedService:
        return GatedService(ServiceConfig(name=f"cluster_node_{idx}"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_services) as executor:
        futures = [executor.submit(init_service, i) for i in range(num_services)]
        services = [f.result() for f in futures]

    # Verify each service resolved its own bound extension instance
    for i, svc in enumerate(services):
        assert isinstance(svc.gate, CountingExtension)
        assert svc.gate is not gate_spec
        assert svc.gate._origin is gate_spec

        # Mutate instance call counts
        svc.gate.call_counts["primary_handler"] = (i + 1) * 10
        svc.gate.call_counts["secondary_handler"] = 0

    # Cross-assert isolation between all service pairs
    for i, svc in enumerate(services):
        assert svc.gate.call_counts["primary_handler"] == (i + 1) * 10
        assert svc.gate.call_counts["secondary_handler"] == 0

    # Verify specification was never modified
    assert gate_spec.call_counts == {}


def test_freeze_immutability_enforcement() -> None:
    """Verify specification immutability under freeze() rejects mutations."""
    spec = ComplexStressExtension(seed=1)
    spec.freeze()

    # Direct attribute reassignment
    with pytest.raises(AttributeError, match="Cannot mutate attribute 'call_log'"):
        spec.call_log = ["illegal"]

    with pytest.raises(AttributeError, match="Cannot mutate attribute 'stats'"):
        spec.stats = {}

    with pytest.raises(AttributeError, match="Cannot mutate attribute 'seed'"):
        spec.seed = 999

    # Assignment to dynamically created attribute
    with pytest.raises(AttributeError, match="Cannot mutate attribute 'new_field'"):
        spec.new_field = "blocked"

    # setattr built-in
    with pytest.raises(AttributeError, match="Cannot mutate attribute 'another_field'"):
        setattr(spec, "another_field", 123)  # noqa: B010

    # Bound copy can mutate attributes freely
    bound = spec.bind("svc_free", "ext")
    assert isinstance(bound, ComplexStressExtension)
    bound.call_log = ["allowed"]
    bound.seed = 777
    assert bound.call_log == ["allowed"]
    assert bound.seed == 777


def test_freeze_immutability_bypass_vectors() -> None:
    """Adversarial checks of the attribute-protocol vectors that freeze() blocks.

    `freeze()` guards the attribute protocol: assignment, `setattr` and deletion
    on a specification raise. A write straight into `__dict__` or through
    `object.__setattr__` does not go through that protocol and is outside what it
    guards. In-place changes to a declared collection are covered separately, by
    the copy `freeze()` takes of the declaration arguments
    (`test_a_frozen_spec_keeps_the_arguments_it_was_declared_with.py`).
    """
    spec = ComplexStressExtension(seed=1)
    spec.freeze()

    # VULNERABILITY 1 FIXED: _spec_frozen cannot be reassigned to unfreeze
    assert spec._spec_frozen is True
    with pytest.raises(AttributeError, match="Cannot mutate attribute '_spec_frozen'"):
        spec._spec_frozen = False

    # VULNERABILITY 2 FIXED: name, service, and _origin cannot be mutated on frozen specifications
    spec2 = ComplexStressExtension(seed=2)
    spec2.freeze()
    with pytest.raises(AttributeError, match="Cannot mutate attribute 'name'"):
        spec2.name = "mutated_spec_name"
    with pytest.raises(AttributeError, match="Cannot mutate attribute 'service'"):
        spec2.service = "mutated_spec_service"
    with pytest.raises(AttributeError, match="Cannot mutate attribute '_origin'"):
        spec2._origin = spec

    # VULNERABILITY 3 FIXED: __delattr__ is guarded, rejecting deletion of attributes
    spec3 = ComplexStressExtension(seed=3)
    spec3.freeze()
    with pytest.raises(AttributeError, match="Cannot delete attribute 'seed'"):
        del spec3.seed
    assert hasattr(spec3, "seed")
