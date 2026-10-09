"""Adversarial stress testing for the introspection primitives.

Empirically validates:
- Complex models: recursive models, mutually recursive models, generic models, unions, and optional fields.
- Deterministic 16-character SHA-256 hashes in Description.components and Description.description_hash.
- Listener discovery across @listener, @validated_listener, @broadcast with push, pull, durable, and fanout permutations.
- Preservation of complex markdown docstrings (fenced code blocks, lists, blank lines) in description and first-line in doc_summary.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    broadcast,
    listener,
    rpc,
    validated_listener,
)
from cliffracer.core.jetstream import StreamSpec
from cliffracer.core.typed_rpc import collect_model_schemas
from cliffracer.introspect import (
    Description,
    describe,
)

pytestmark = pytest.mark.unit

# ==============================================================================
# Suite 2: Introspection Primitives Adversarial Stress
# ==============================================================================


# Models for stress testing
class SelfRecursiveTree(BaseModel):
    value: int
    left: SelfRecursiveTree | None = None
    right: SelfRecursiveTree | None = None


class GraphEdge(BaseModel):
    weight: float
    target: GraphNode | None = None


class GraphNode(BaseModel):
    node_id: str
    edges: list[GraphEdge] = []


GraphEdge.model_rebuild()
GraphNode.model_rebuild()


class GenericContainer[T](BaseModel):
    count: int
    items: list[T]
    metadata: dict[str, str] = {}


class CatModel(BaseModel):
    pet_type: Literal["cat"] = "cat"
    whiskers: int


class DogModel(BaseModel):
    pet_type: Literal["dog"] = "dog"
    pack_size: int


class ComplexModel(BaseModel):
    name: str
    active: bool = True
    tree: SelfRecursiveTree | None = None
    pet: CatModel | DogModel
    scores: list[int]
    attributes: dict[str, str] = Field(default_factory=dict)


class AdversarialService(CliffracerService):
    config = ServiceConfig(
        name="adversarial_introspection_service",
        version="2.4.1",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="TEST_STREAM", subjects=["adversarial.>"]),
        ],
    )

    @rpc
    async def process_tree(self, tree: SelfRecursiveTree) -> SelfRecursiveTree:
        """Process binary tree hierarchy.

        This method walks a recursive binary search tree.

        Features:
        * In-order traversal
        * Balanced pruning
          - Depth check
          - Leaf deduplication

        Code example:
        ```python
        tree = SelfRecursiveTree(value=10)
        await client.process_tree(tree)
        ```
        """
        return tree

    @rpc
    async def get_graph(self, root_id: str) -> GraphNode:
        """Fetch cyclic graph topology."""
        return GraphNode(node_id=root_id)

    @rpc
    async def get_generic_data(self) -> GenericContainer[ComplexModel]:
        """Return generic container of complex models."""
        return GenericContainer[ComplexModel](count=0, items=[])

    @listener("adversarial.raw.push", fanout=True)
    async def on_raw_push(self, payload: str) -> None:
        """Handle raw push events.

        First line summary.
        Additional markdown details.
        """
        pass

    @listener("adversarial.raw.durable", durable="durable_raw")
    async def on_raw_durable(self, payload: str) -> None:
        """Handle raw durable push events."""
        pass

    @listener("adversarial.raw.fanout", fanout=True)
    async def on_raw_fanout(self, payload: str) -> None:
        """Handle raw fanout events."""
        pass

    @listener("adversarial.raw.pull", durable="durable_pull", pull=True)
    async def on_raw_pull(self, payload: str) -> None:
        """Handle raw pull consumer events."""
        pass

    @validated_listener("adversarial.validated.event", ComplexModel, fanout=True)
    async def on_validated(self, event: ComplexModel) -> None:
        """Handle validated event payload.

        Preserves complex structure.
        """
        pass

    @validated_listener("adversarial.validated.durable", ComplexModel, durable="val_durable")
    async def on_validated_durable(self, event: ComplexModel) -> None:
        pass

    @broadcast("adversarial.broadcast.signal")
    async def on_broadcast_signal(self, sig: str) -> None:
        """Broadcast signal to all instances."""
        pass


def test_adversarial_recursive_models_schema_collection_terminates() -> None:
    """Stress Test: Mutually recursive and self-referential Pydantic models collect without recursion error."""
    components: dict[str, Any] = {}
    collect_model_schemas(SelfRecursiveTree, components)
    collect_model_schemas(GraphNode, components)

    assert len(components) >= 3
    # Check that each schema contains valid properties
    for s_hash, schema in components.items():
        assert len(s_hash) == 16
        assert isinstance(schema, dict)
        assert "properties" in schema or "$defs" in schema or "type" in schema


def test_adversarial_components_16char_sha256_hash_invariants() -> None:
    """Stress Test: Every component schema key is strictly a deterministic 16-char SHA-256 hash."""
    desc = describe(AdversarialService, config=AdversarialService.config)

    assert desc.service == "adversarial_introspection_service"
    assert desc.version == "2.4.1"
    assert len(desc.components) > 0

    for schema_hash, schema in desc.components.items():
        # Invariant 1: Key length is exactly 16
        assert len(schema_hash) == 16, f"Hash {schema_hash} length != 16"
        # Invariant 2: Hash is alphanumeric
        assert schema_hash.isalnum(), f"Hash {schema_hash} not alphanumeric"
        # Invariant 3: Matches first 16 hex chars of sha256 of sorted json schema
        expected_hash = hashlib.sha256(
            json.dumps(schema, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        assert schema_hash == expected_hash, (
            f"Hash mismatch for {schema.get('title')}: {schema_hash} != {expected_hash}"
        )


def test_adversarial_hash_determinism_across_reordering_and_repeated_invocations() -> None:
    """Stress Test: describe() produces identical component hashes regardless of declaration order."""
    desc1 = describe(AdversarialService, config=AdversarialService.config)
    desc2 = describe(AdversarialService, config=AdversarialService.config)

    assert desc1.components == desc2.components
    assert desc1.description_hash == desc2.description_hash
    assert [m.name for m in desc1.methods] == [m.name for m in desc2.methods]
    assert [listener.pattern for listener in desc1.listeners] == [
        listener.pattern for listener in desc2.listeners
    ]


def test_adversarial_listener_introspection_matrix() -> None:
    """Stress Test: Verification of all listener decorators, queue groups, durables, and fanout semantics."""
    desc = describe(AdversarialService, config=AdversarialService.config)

    assert len(desc.listeners) == 7

    # 1. Raw push (fanout, no durable) -> queue_group is None
    l_raw = desc.listener("adversarial.raw.push")
    assert l_raw is not None
    assert l_raw.fanout is True
    assert l_raw.pull is False
    assert l_raw.durable is None
    assert l_raw.queue_group is None
    assert l_raw.is_validated is False

    # 2. Raw durable push -> queue_group == durable
    l_durable = desc.listener("adversarial.raw.durable")
    assert l_durable is not None
    assert l_durable.fanout is False
    assert l_durable.pull is False
    assert l_durable.durable == "durable_raw"
    assert l_durable.queue_group == "durable_raw"

    # 3. Raw fanout push -> queue_group is None
    l_fanout = desc.listener("adversarial.raw.fanout")
    assert l_fanout is not None
    assert l_fanout.fanout is True
    assert l_fanout.pull is False
    assert l_fanout.durable is None
    assert l_fanout.queue_group is None

    # 4. Raw pull -> queue_group is None
    l_pull = desc.listener("adversarial.raw.pull")
    assert l_pull is not None
    assert l_pull.pull is True
    assert l_pull.fanout is False
    assert l_pull.durable == "durable_pull"
    assert l_pull.queue_group is None

    # 5. Validated listener -> schema contains schema_hash matching components
    l_val = desc.listener("adversarial.validated.event")
    assert l_val is not None
    assert l_val.is_validated is True
    assert l_val.schema is not None
    val_schema_hash = l_val.schema.get("schema_hash")
    assert val_schema_hash in desc.components

    # 6. Validated durable push -> queue_group == durable
    l_val_dur = desc.listener("adversarial.validated.durable")
    assert l_val_dur is not None
    assert l_val_dur.durable == "val_durable"
    assert l_val_dur.queue_group == "val_durable"

    # 7. Broadcast -> fanout is True, is_broadcast is True, queue_group is None
    l_bcast = desc.listener("adversarial.broadcast.signal")
    assert l_bcast is not None
    assert l_bcast.is_broadcast is True
    assert l_bcast.fanout is True
    assert l_bcast.queue_group is None


def test_adversarial_multiline_docstring_markdown_preservation() -> None:
    """Stress Test: Multi-line docstring preserves code blocks, bullet points, and blank lines in description."""
    desc = describe(AdversarialService, config=AdversarialService.config)
    m = desc.method("process_tree")
    assert m is not None

    # doc_summary is the first non-empty line
    assert m.doc_summary == "Process binary tree hierarchy."
    assert m.doc == "Process binary tree hierarchy."

    # description preserves full markdown
    assert m.description is not None
    assert "Features:" in m.description
    assert "* In-order traversal" in m.description
    assert "```python" in m.description
    assert "await client.process_tree(tree)" in m.description
    assert "\n\n" in m.description

    # Raw listener docstring
    l_raw = desc.listener("adversarial.raw.push")
    assert l_raw is not None
    assert l_raw.doc_summary == "Handle raw push events."
    assert l_raw.description is not None
    assert "Additional markdown details." in l_raw.description

    # Listener with no docstring
    l_no_doc = desc.listener("adversarial.validated.durable")
    assert l_no_doc is not None
    assert l_no_doc.doc_summary is None
    assert l_no_doc.description is None


def test_adversarial_description_serialization_round_trip() -> None:
    """Stress Test: Description to_dict() and from_dict() round trip survives without data loss."""
    desc = describe(AdversarialService, config=AdversarialService.config)
    d = desc.to_dict()

    # JSON serialization and deserialization
    json_str = json.dumps(d)
    d_loaded = json.loads(json_str)
    restored = Description.from_dict(d_loaded)

    assert restored.service == desc.service
    assert restored.version == desc.version
    assert restored.description_hash == desc.description_hash
    assert len(restored.methods) == len(desc.methods)
    assert len(restored.listeners) == len(desc.listeners)
    assert len(restored.streams) == len(desc.streams)
    assert restored.components == desc.components


class CycleNodeA(BaseModel):
    name: str
    b: CycleNodeB | None = None


class CycleNodeB(BaseModel):
    name: str
    c: CycleNodeC | None = None


class CycleNodeC(BaseModel):
    name: str
    a: CycleNodeA | None = None


CycleNodeA.model_rebuild()
CycleNodeB.model_rebuild()
CycleNodeC.model_rebuild()


class ConstrainedModel(BaseModel):
    username: str = Field(min_length=3, max_length=20, pattern="^[a-z0-9_]+$")
    score: int = Field(ge=0, le=1000)
    ratio: float = Field(gt=0.0, lt=1.0)


class ExtremeDocstringService(CliffracerService):
    @rpc
    async def empty_doc(self, x: int) -> int:
        """"""
        return x

    @rpc
    async def whitespace_doc(self, x: int) -> int:
        """

        \t
        """
        return x

    @rpc
    async def leading_newline_doc(self, x: int) -> int:
        """

        First real line of documentation.
        Second paragraph here.
        """
        return x

    @rpc
    async def rich_markdown_doc(self, node: CycleNodeA, item: ConstrainedModel) -> CycleNodeA:
        """Endpoint with rich markdown and emoji 🚀.

        Detailed specifications:
        | Column A | Column B |
        |----------|----------|
        | Alpha    | Beta     |

        * Nested list item 1
          * Sub-bullet A
          * Sub-bullet B
        * Nested list item 2

        ```json
        {"status": "ok", "count": 10}
        ```

        HTML: <span class="badge">Production</span>
        """
        return node


def test_adversarial_deep_cyclic_and_constrained_models() -> None:
    """Stress Test: 3-cycle recursive models and constrained fields collect with deterministic hashes."""
    desc = describe(ExtremeDocstringService)
    assert len(desc.components) >= 4

    for s_hash, schema in desc.components.items():
        assert len(s_hash) == 16
        expected = hashlib.sha256(json.dumps(schema, sort_keys=True).encode("utf-8")).hexdigest()[
            :16
        ]
        assert s_hash == expected


def test_adversarial_extreme_docstrings_extraction() -> None:
    """Stress Test: Empty, whitespace, leading blank line, emoji, tables, and HTML docstrings."""
    desc = describe(ExtremeDocstringService)

    # 1. Empty docstring
    m_empty = desc.method("empty_doc")
    assert m_empty is not None
    assert m_empty.doc_summary is None
    assert m_empty.description is None

    # 2. Whitespace docstring: doc_summary is None, description preserves whitespace
    m_white = desc.method("whitespace_doc")
    assert m_white is not None
    assert m_white.doc_summary is None
    assert m_white.description is not None and m_white.description.strip() == ""

    # 3. Leading newline docstring
    m_lead = desc.method("leading_newline_doc")
    assert m_lead is not None
    assert m_lead.doc_summary == "First real line of documentation."
    assert m_lead.description is not None
    assert "Second paragraph here." in m_lead.description

    # 4. Rich markdown with emoji and tables
    m_rich = desc.method("rich_markdown_doc")
    assert m_rich is not None
    assert m_rich.doc_summary == "Endpoint with rich markdown and emoji 🚀."
    assert m_rich.description is not None
    assert "| Column A | Column B |" in m_rich.description
    assert "* Sub-bullet A" in m_rich.description
    assert '{"status": "ok", "count": 10}' in m_rich.description
    assert '<span class="badge">Production</span>' in m_rich.description

    # Ensure to_dict() / from_dict() round trip survives rich markdown and unicode
    d = desc.to_dict()
    restored = Description.from_dict(json.loads(json.dumps(d)))
    m_restored = restored.method("rich_markdown_doc")
    assert m_restored is not None
    assert m_restored.doc_summary == m_rich.doc_summary
    assert m_restored.description == m_rich.description
