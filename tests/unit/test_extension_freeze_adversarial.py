"""Adversarial stress tests for Extension.freeze() hardening and bypass vectors.

Empirically validates:
1. Immutability of frozen extension specifications.
2. Prevention of unfreezing via _spec_frozen reassignment.
3. Prevention of mutation on identity attributes (name, service, _origin, fails_closed).
4. Prevention of attribute deletion via del and delattr (__delattr__ guard).
5. Full runtime mutability on bound runtime instances (bind()).
6. State isolation across multiple bound instances during mutation and deletion.
7. Idempotency behavior of freeze() calls.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit


class SampleStressExtension(Extension):
    """Extension featuring scalar and nested mutable state."""

    def __init__(self, seed: int = 10) -> None:
        self.seed = seed
        self.items: list[str] = [f"item_{seed}"]
        self.metadata: dict[str, Any] = {"version": 1, "nested": {"counter": 0}}
        self.tags: set[str] = {"tag_a", "tag_b"}


def test_freeze_prevents_unfreezing_reassignment() -> None:
    """Verify that _spec_frozen cannot be reassigned to False or any falsy value."""
    spec = SampleStressExtension(seed=1)
    spec.freeze()
    assert spec._spec_frozen is True

    # Test resetting to False
    with pytest.raises(AttributeError, match="Cannot mutate attribute '_spec_frozen'"):
        spec._spec_frozen = False

    # Test resetting via setattr
    with pytest.raises(AttributeError, match="Cannot mutate attribute '_spec_frozen'"):
        setattr(spec, "_spec_frozen", False)  # noqa: B010

    # Test resetting to None or falsy ints
    with pytest.raises(AttributeError, match="Cannot mutate attribute '_spec_frozen'"):
        setattr(spec, "_spec_frozen", None)  # noqa: B010

    with pytest.raises(AttributeError, match="Cannot mutate attribute '_spec_frozen'"):
        setattr(spec, "_spec_frozen", 0)  # noqa: B010

    # Flag must remain strictly True
    assert spec._spec_frozen is True


def test_freeze_prevents_mutating_identity_and_spec_attributes() -> None:
    """Verify that name, service, _origin, fails_closed, and spec args cannot be mutated."""
    spec = SampleStressExtension(seed=2)
    spec.freeze()

    # Identity attributes
    with pytest.raises(AttributeError, match="Cannot mutate attribute 'name'"):
        spec.name = "new_name"

    with pytest.raises(AttributeError, match="Cannot mutate attribute 'service'"):
        spec.service = "mock_service"

    with pytest.raises(AttributeError, match="Cannot mutate attribute '_origin'"):
        spec._origin = spec

    with pytest.raises(AttributeError, match="Cannot mutate attribute 'fails_closed'"):
        spec.fails_closed = True

    # Spec internal attributes
    with pytest.raises(AttributeError, match="Cannot mutate attribute '_spec_args'"):
        spec._spec_args = (99,)

    with pytest.raises(AttributeError, match="Cannot mutate attribute '_spec_kwargs'"):
        spec._spec_kwargs = {"seed": 99}

    # Custom attributes defined on class/instance
    with pytest.raises(AttributeError, match="Cannot mutate attribute 'seed'"):
        spec.seed = 999

    with pytest.raises(AttributeError, match="Cannot mutate attribute 'items'"):
        spec.items = []

    with pytest.raises(AttributeError, match="Cannot mutate attribute 'metadata'"):
        spec.metadata = {}

    with pytest.raises(AttributeError, match="Cannot mutate attribute 'tags'"):
        spec.tags = set()

    # Dynamic attribute creation
    spec_any: Any = spec
    with pytest.raises(AttributeError, match="Cannot mutate attribute 'dynamic_attr'"):
        spec_any.dynamic_attr = "injected"

    with pytest.raises(AttributeError, match="Cannot mutate attribute 'dynamic_attr'"):
        setattr(spec, "dynamic_attr", 123)  # noqa: B010


def test_freeze_prevents_attribute_deletion() -> None:
    """Verify that __delattr__ blocks deletion of attributes on frozen specifications."""
    spec = SampleStressExtension(seed=3)
    spec.freeze()

    # Attempting to delete the freeze flag itself to unlock the instance
    with pytest.raises(AttributeError, match="Cannot delete attribute '_spec_frozen'"):
        del spec._spec_frozen

    with pytest.raises(AttributeError, match="Cannot delete attribute '_spec_frozen'"):
        delattr(spec, "_spec_frozen")

    assert hasattr(spec, "_spec_frozen")
    assert spec._spec_frozen is True

    # Attempting to delete instance attributes
    with pytest.raises(AttributeError, match="Cannot delete attribute 'seed'"):
        del spec.seed

    with pytest.raises(AttributeError, match="Cannot delete attribute 'items'"):
        del spec.items

    with pytest.raises(AttributeError, match="Cannot delete attribute 'metadata'"):
        delattr(spec, "metadata")

    # Attempting to delete identity attributes
    with pytest.raises(AttributeError, match="Cannot delete attribute 'name'"):
        del spec.name

    with pytest.raises(AttributeError, match="Cannot delete attribute 'service'"):
        del spec.service

    with pytest.raises(AttributeError, match="Cannot delete attribute '_origin'"):
        del spec._origin

    with pytest.raises(AttributeError, match="Cannot delete attribute 'fails_closed'"):
        del spec.fails_closed

    with pytest.raises(AttributeError, match="Cannot delete attribute '_spec_args'"):
        del spec._spec_args

    with pytest.raises(AttributeError, match="Cannot delete attribute '_spec_kwargs'"):
        del spec._spec_kwargs

    # Attempting to delete non-existent attribute
    spec_any: Any = spec
    with pytest.raises(AttributeError, match="Cannot delete attribute 'non_existent_field'"):
        del spec_any.non_existent_field

    with pytest.raises(AttributeError, match="Cannot delete attribute 'non_existent_field'"):
        delattr(spec, "non_existent_field")


def test_bound_instances_retain_full_runtime_mutability() -> None:
    """Verify that bound instances are not frozen and can mutate, add, and delete attributes."""
    spec = SampleStressExtension(seed=4)
    spec.freeze()

    dummy_service = object()
    bound = spec.bind(dummy_service, "test_bound_ext")

    assert isinstance(bound, SampleStressExtension)
    assert bound is not spec
    assert bound._origin is spec
    assert bound.service is dummy_service
    assert bound.name == "test_bound_ext"
    assert bound._spec_frozen is False

    # 1. Mutate existing attributes
    bound.seed = 42
    assert bound.seed == 42
    bound.items.append("bound_item")
    bound.items = ["overridden_list"]
    assert bound.items == ["overridden_list"]
    bound.metadata["version"] = 2
    bound.tags.add("bound_tag")

    # 2. Add dynamic attributes
    bound_any: Any = bound
    bound_any.runtime_cache = {"token": "xyz"}
    assert bound_any.runtime_cache == {"token": "xyz"}
    setattr(bound, "dynamic_prop", [1, 2, 3])  # noqa: B010
    assert bound_any.dynamic_prop == [1, 2, 3]

    # 3. Mutate identity attributes
    bound.name = "renamed_ext"
    assert bound.name == "renamed_ext"
    bound.service = "new_service"
    assert bound.service == "new_service"

    # 4. Delete attributes
    del bound_any.dynamic_prop
    assert not hasattr(bound, "dynamic_prop")
    delattr(bound, "runtime_cache")
    assert not hasattr(bound, "runtime_cache")
    del bound.seed
    assert not hasattr(bound, "seed")

    # 5. Verify specification was completely unpolluted
    assert spec.seed == 4
    assert spec.items == ["item_4"]
    assert spec.metadata == {"version": 1, "nested": {"counter": 0}}
    assert spec.tags == {"tag_a", "tag_b"}
    assert spec.name == ""
    assert spec.service is None
    assert not hasattr(spec, "dynamic_prop")
    assert not hasattr(spec, "runtime_cache")


def test_multi_bound_isolation_under_adversarial_mutations() -> None:
    """Verify multiple bound instances can mutate and delete attributes without interference."""
    spec = SampleStressExtension(seed=5)
    spec.freeze()

    svc_a, svc_b, svc_c = "svc_a", "svc_b", "svc_c"
    bound_a = cast(SampleStressExtension, spec.bind(svc_a, "ext_a"))
    bound_b = cast(SampleStressExtension, spec.bind(svc_b, "ext_b"))
    bound_c = cast(SampleStressExtension, spec.bind(svc_c, "ext_c"))

    assert isinstance(bound_a, SampleStressExtension)
    assert isinstance(bound_b, SampleStressExtension)
    assert isinstance(bound_c, SampleStressExtension)

    # Perform differing mutations across all 3
    bound_a_any: Any = bound_a
    bound_b_any: Any = bound_b
    bound_c_any: Any = bound_c

    bound_a.seed = 100
    bound_a.items.append("from_a")
    bound_a.tags.add("tag_from_a")
    bound_a_any.custom_state = "state_a"

    bound_b.seed = 200
    bound_b.items = ["replaced_by_b"]
    del bound_b.metadata
    bound_b_any.custom_state = "state_b"

    bound_c.seed = 300
    bound_c.items.append("from_c")
    del bound_c.seed  # Delete seed on instance c only

    # Assert bound_a state
    assert bound_a.seed == 100
    assert bound_a.items == ["item_5", "from_a"]
    assert "tag_from_a" in bound_a.tags
    assert hasattr(bound_a, "metadata")
    assert bound_a_any.custom_state == "state_a"

    # Assert bound_b state
    assert bound_b.seed == 200
    assert bound_b.items == ["replaced_by_b"]
    assert "tag_from_a" not in bound_b.tags
    assert not hasattr(bound_b, "metadata")
    assert bound_b_any.custom_state == "state_b"

    # Assert bound_c state
    assert not hasattr(bound_c, "seed")
    assert bound_c.items == ["item_5", "from_c"]
    assert hasattr(bound_c, "metadata")
    assert not hasattr(bound_c_any, "custom_state")

    # Assert specification state
    assert spec.seed == 5
    assert spec.items == ["item_5"]
    assert hasattr(spec, "metadata")
    assert not hasattr(spec, "custom_state")


def test_freeze_idempotence_and_bound_freezing() -> None:
    """Test calling freeze() on already frozen spec and on bound instances."""
    spec = SampleStressExtension(seed=6)
    spec.freeze()

    # Calling freeze() a second time on an already frozen spec:
    # Because self._spec_frozen is already True, self._spec_frozen = True
    # in freeze() triggers __setattr__, raising AttributeError!
    with pytest.raises(AttributeError, match="Cannot mutate attribute '_spec_frozen'"):
        spec.freeze()

    # Freezing a bound instance:
    bound: Any = spec.bind("svc", "ext")
    bound.custom = 1
    bound.freeze()

    # Now bound instance is also locked
    with pytest.raises(AttributeError, match="Cannot mutate attribute 'custom'"):
        bound.custom = 2

    with pytest.raises(AttributeError, match="Cannot delete attribute 'custom'"):
        del bound.custom
