"""Adversarial stress tests for Extension factory multi-threaded and multi-task isolation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import (
    Extension,
    ExtensionSetupContext,
    WorkerContext,
    _safe_clone_arg,
    entrypoint,
)

pytestmark = pytest.mark.unit


class SubNode:
    """Nested state node to verify deep copying and instance isolation."""

    def __init__(self, val: int = 0) -> None:
        self.val = val
        self.items: list[str] = []


class HighlyConcurrentExtension(Extension):
    """Extension containing diverse mutable data structures and nested extensions."""

    def __init__(
        self,
        base_id: int = 0,
        config_map: dict[str, Any] | None = None,
        tag_list: list[str] | None = None,
        tuple_matrix: tuple[list[int], ...] | None = None,
    ) -> None:
        self.base_id = base_id
        self.config_map = config_map if config_map is not None else {}
        self.tag_list = tag_list if tag_list is not None else []
        self.tuple_matrix = tuple_matrix if tuple_matrix is not None else ()
        self.local_counter = 0
        self.history: list[str] = []
        self.sub_node = SubNode(base_id)

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        self.history.append(f"setup:{ctx.service_config.name}")

    async def worker_setup(self, ctx: WorkerContext) -> None:
        self.local_counter += 1
        self.history.append(f"worker_setup:{ctx.kind}")
        ctx.data[f"ext_data_{self.name}"] = f"processed_{self.local_counter}"

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        self.history.append(f"worker_teardown:{ctx.kind}")


class NestedChildExtension(Extension):
    """Child extension nested inside another extension."""

    def __init__(self, prefix: str = "child") -> None:
        self.prefix = prefix
        self.child_log: list[str] = []


class ParentWithNestedExtension(Extension):
    """Parent extension holding an inner child extension."""

    def __init__(self, child: NestedChildExtension | None = None) -> None:
        self.child = child or NestedChildExtension()
        self.parent_log: list[str] = []


def test_massive_multithreaded_concurrent_bind_and_mutation() -> None:
    """Stress-test extension state isolation across 100 concurrent worker threads."""
    spec = HighlyConcurrentExtension(
        base_id=42,
        config_map={"env": "prod", "flags": [1, 2, 3]},
        tag_list=["init_a", "init_b"],
        tuple_matrix=([10, 20], [30, 40]),
    )
    spec.freeze()

    num_threads = 100
    mutations_per_thread = 100

    def thread_worker(thread_id: int) -> HighlyConcurrentExtension:
        service_id = f"thread_svc_{thread_id}"
        bound = spec.bind(service_id, f"ext_{thread_id}")

        assert isinstance(bound, HighlyConcurrentExtension)
        assert bound._origin is spec
        assert bound.service == service_id
        assert bound._spec_frozen is False

        for step in range(mutations_per_thread):
            bound.tag_list.append(f"t_{thread_id}_{step}")
            bound.config_map[f"key_{thread_id}_{step}"] = step
            bound.tuple_matrix[0].append(thread_id * 1000 + step)
            bound.history.append(f"hist_{thread_id}_{step}")
            bound.sub_node.items.append(f"node_{thread_id}_{step}")
            bound.sub_node.val += 1

        return bound

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = [executor.submit(thread_worker, i) for i in range(num_threads)]
        bound_instances = [f.result() for f in futures]

    # Verify per-instance complete data isolation across all 100 threads
    for i, bound in enumerate(bound_instances):
        assert len(bound.tag_list) == 2 + mutations_per_thread
        assert bound.tag_list[:2] == ["init_a", "init_b"]
        assert all(item.startswith(f"t_{i}_") for item in bound.tag_list[2:])

        assert len(bound.config_map) == 2 + mutations_per_thread
        assert all(k.startswith(f"key_{i}_") for k in bound.config_map if k not in ("env", "flags"))

        assert len(bound.tuple_matrix[0]) == 2 + mutations_per_thread
        assert bound.tuple_matrix[1] == [30, 40]

        assert len(bound.history) == mutations_per_thread
        assert all(h.startswith(f"hist_{i}_") for h in bound.history)

        assert bound.sub_node.val == 42 + mutations_per_thread
        assert len(bound.sub_node.items) == mutations_per_thread
        assert all(item.startswith(f"node_{i}_") for item in bound.sub_node.items)

    # Verify specification was never mutated
    assert spec.tag_list == ["init_a", "init_b"]
    assert spec.config_map == {"env": "prod", "flags": [1, 2, 3]}
    assert spec.tuple_matrix == ([10, 20], [30, 40])
    assert spec.history == []
    assert spec.sub_node.val == 42
    assert spec.sub_node.items == []


def _run_freeze_race_iteration(iteration: int) -> None:
    """Execute a single race round between concurrent bind() calls and freeze()."""
    spec = HighlyConcurrentExtension(
        base_id=iteration,
        config_map={"iter": iteration},
        tag_list=[f"tag_{iteration}"],
    )

    num_threads = 40
    barrier = threading.Barrier(num_threads + 1)
    bound_results: list[HighlyConcurrentExtension] = []
    lock = threading.Lock()

    def binder_worker(thread_id: int) -> None:
        barrier.wait()
        bound = spec.bind(f"svc_{thread_id}", f"ext_{thread_id}")
        assert isinstance(bound, HighlyConcurrentExtension)
        assert bound._spec_frozen is False
        bound.tag_list.append(f"worker_{thread_id}")
        with lock:
            bound_results.append(bound)

    def freezer_worker() -> None:
        barrier.wait()
        spec.freeze()

    threads = [threading.Thread(target=binder_worker, args=(i,)) for i in range(num_threads)]
    freezer = threading.Thread(target=freezer_worker)

    for t in threads:
        t.start()
    freezer.start()

    for t in threads:
        t.join()
    freezer.join()

    assert len(bound_results) == num_threads
    assert spec._spec_frozen is True
    # Spec must not have any appended worker tags
    assert spec.tag_list == [f"tag_{iteration}"]

    # Every bound copy must be independent
    for bound in bound_results:
        assert len(bound.tag_list) == 2
        assert bound.tag_list[0] == f"tag_{iteration}"
        assert bound.tag_list[1].startswith("worker_")


def test_concurrent_bind_racing_with_dynamic_freeze() -> None:
    """Stress-test concurrent bind() calls racing against freeze() execution."""
    for iteration in range(5):
        _run_freeze_race_iteration(iteration)


@pytest.mark.asyncio
async def test_multitask_asyncio_concurrent_hook_execution_stress() -> None:
    """Stress-test concurrent hook execution across 60 interleaved asyncio tasks."""
    spec = HighlyConcurrentExtension(base_id=1)
    spec.freeze()

    num_tasks = 60
    cycles_per_task = 20

    async def task_worker(task_id: int) -> HighlyConcurrentExtension:
        service_id = f"async_svc_{task_id}"
        bound = spec.bind(service_id, f"ext_{task_id}")
        assert isinstance(bound, HighlyConcurrentExtension)

        setup_ctx = ExtensionSetupContext(
            service_config=ServiceConfig(name=service_id),
            broker_url="nats://localhost:4222",
            service=None,
        )
        await bound.setup(setup_ctx)

        for c in range(cycles_per_task):
            worker_ctx = WorkerContext(
                kind=f"rpc_call_{task_id}",
                subject=f"test.subject.{task_id}",
                headers={"x-task": str(task_id)},
                correlation_id=f"cid-{task_id}-{c}",
                payload={"data": c},
            )
            await bound.worker_setup(worker_ctx)
            await asyncio.sleep(0)  # Force cooperative multitasking yield
            assert worker_ctx.data[f"ext_data_{bound.name}"] == f"processed_{c + 1}"
            await bound.worker_teardown(worker_ctx)

        return bound

    tasks = [asyncio.create_task(task_worker(i)) for i in range(num_tasks)]
    instances = await asyncio.gather(*tasks)

    for i, inst in enumerate(instances):
        assert inst.local_counter == cycles_per_task
        assert inst.history[0] == f"setup:async_svc_{i}"
        # 1 setup + 20 * 2 hooks = 41 entries
        assert len(inst.history) == 1 + (cycles_per_task * 2)

    assert spec.local_counter == 0
    assert spec.history == []


def test_unfrozen_spec_concurrent_bind_preserves_spec_arguments() -> None:
    """Verify that an unfrozen specification preserves its declaration arguments under concurrent bind."""
    spec = HighlyConcurrentExtension(
        base_id=77,
        config_map={"unfrozen": True},
        tag_list=["keep_clean"],
    )
    assert spec._spec_frozen is False

    num_threads = 50

    def worker(idx: int) -> None:
        bound = spec.bind(f"svc_{idx}", f"ext_{idx}")
        assert isinstance(bound, HighlyConcurrentExtension)
        bound.config_map["mutated_by"] = idx
        bound.tag_list.append(f"leaked_{idx}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(worker, i) for i in range(num_threads)]
        concurrent.futures.wait(futures)

    assert spec._spec_frozen is False
    assert spec.config_map == {"unfrozen": True}
    assert spec.tag_list == ["keep_clean"]
    assert "mutated_by" not in spec.config_map
    assert len(spec.tag_list) == 1


def test_nested_extension_cloning_isolation_under_concurrency() -> None:
    """Verify nested extensions passed as constructor arguments are cloned independently."""
    child_spec = NestedChildExtension(prefix="shared_inner")
    child_spec.freeze()
    parent_spec = ParentWithNestedExtension(child=child_spec)
    parent_spec.freeze()

    num_threads = 40

    def worker(idx: int) -> tuple[ParentWithNestedExtension, NestedChildExtension]:
        bound_parent = parent_spec.bind(f"svc_{idx}", f"parent_{idx}")
        assert isinstance(bound_parent, ParentWithNestedExtension)
        assert isinstance(bound_parent.child, NestedChildExtension)
        bound_parent.parent_log.append(f"p_{idx}")
        bound_parent.child.child_log.append(f"c_{idx}")
        return bound_parent, bound_parent.child

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(worker, i) for i in range(num_threads)]
        results = [f.result() for f in futures]

    for i, (parent, child) in enumerate(results):
        assert parent.parent_log == [f"p_{i}"]
        assert child.child_log == [f"c_{i}"]
        assert child is not child_spec
        # Cloned inner extensions created via create_instance have _origin=None (only directly bound extensions have _origin)
        assert child._origin is None

    assert parent_spec.parent_log == []
    assert child_spec.child_log == []


def test_safe_clone_arg_edge_cases() -> None:
    """Verify _safe_clone_arg handles deeply nested and edge-case argument types."""
    # 1. Tuple containing nested lists
    data_tuple = ([1, [2, 3]], {"k": [4, 5]}, {6, 7})
    cloned_tuple = _safe_clone_arg(data_tuple)
    assert cloned_tuple == data_tuple
    assert cloned_tuple is not data_tuple
    assert cloned_tuple[0] is not data_tuple[0]
    assert cloned_tuple[1] is not data_tuple[1]
    assert cloned_tuple[2] is not data_tuple[2]

    # Mutate clone
    cloned_tuple[0][1].append(99)
    cloned_tuple[1]["k"].append(88)
    cloned_tuple[2].add(77)

    assert data_tuple[0][1] == [2, 3]
    assert data_tuple[1]["k"] == [4, 5]
    assert data_tuple[2] == {6, 7}

    # 2. Extension instance cloning via _safe_clone_arg
    child = NestedChildExtension(prefix="clone_me")
    cloned_child = _safe_clone_arg(child)
    assert isinstance(cloned_child, NestedChildExtension)
    assert cloned_child is not child
    assert cloned_child.prefix == "clone_me"

    # 3. Scalar types return directly
    assert _safe_clone_arg(42) == 42
    assert _safe_clone_arg("text") == "text"
    assert _safe_clone_arg(None) is None


def test_entrypoint_discovery_thread_safety_stress() -> None:
    """Verify concurrent instantiation and discovery across multiple CliffracerService classes."""

    class CustomGateExtension(Extension):
        def __init__(self) -> None:
            self.registry: list[tuple[str, str]] = []

        def entrypoint_kinds(self) -> dict[str, Any]:
            def _binder(
                service: Any, method_name: str, bound_method: Any, spec: dict[str, Any]
            ) -> None:
                self.registry.append((service.config.name, method_name))

            return {"concurrency_gate": _binder}

    gate_spec = CustomGateExtension()
    gate_spec.freeze()

    class SvcA(CliffracerService):
        gate = gate_spec

        @entrypoint("concurrency_gate", owner=gate_spec)
        async def handle_a(self) -> str:
            return "a"

    class SvcB(CliffracerService):
        gate = gate_spec

        @entrypoint("concurrency_gate", owner=gate_spec)
        async def handle_b(self) -> str:
            return "b"

    num_threads = 40

    def worker(idx: int) -> tuple[CliffracerService, list[tuple[str, str]]]:
        cls: type[SvcA] | type[SvcB] = SvcA if idx % 2 == 0 else SvcB
        expected_method = "handle_a" if idx % 2 == 0 else "handle_b"
        svc: CliffracerService = cls(ServiceConfig(name=f"svc_thread_{idx}"))
        svc._discover_handlers()
        gate_ext = getattr(svc, "gate")  # noqa: B009
        assert isinstance(gate_ext, CustomGateExtension)
        assert gate_ext is not gate_spec
        return svc, [(f"svc_thread_{idx}", expected_method)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(worker, i) for i in range(num_threads)]
        results = [f.result() for f in futures]

    for svc, expected in results:
        gate_ext = getattr(svc, "gate")  # noqa: B009
        assert isinstance(gate_ext, CustomGateExtension)
        assert gate_ext.registry == expected

    assert gate_spec.registry == []
