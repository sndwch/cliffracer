"""Unit test suite for Introspection Primitives.

Verifies:
- Inclusion of full Pydantic JSON schemas in Description.components
- Expansion of describe() to introspect @listener, @validated_listener, @broadcast, and JetStream streams
- Preservation of full multi-line docstrings in HandlerSpec and Method metadata
"""

from __future__ import annotations

import hashlib
import json

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
from cliffracer.core.typed_rpc import build_handler_spec, collect_model_schemas
from cliffracer.introspect import (
    Description,
    canonical,
    describe,
)


# ---------------------------------------------------------------------------
# Models for Testing
# ---------------------------------------------------------------------------
class SubDetail(BaseModel):
    tag: str
    weight: float = 1.0


class SubItem(BaseModel):
    name: str
    detail: SubDetail


class ComplexRequest(BaseModel):
    req_id: str
    sub_items: list[SubItem]
    extra: dict[str, SubDetail] | None = None


class ComplexResponse(BaseModel):
    status: str
    primary_detail: SubDetail


class EventPayload(BaseModel):
    event_id: str
    payload_tag: str


class CyclicTree(BaseModel):
    name: str
    children: list[CyclicTree] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Service Fixture
# ---------------------------------------------------------------------------
class ComprehensiveService(CliffracerService):
    """A comprehensive test service exercising all introspection primitives."""

    @rpc
    async def execute_task(self, req: ComplexRequest) -> ComplexResponse:
        """Execute complex business task.

        Detailed notes:
        - Validates sub_items and detail recursively.
        - Returns a primary detail confirmation.
        """
        return ComplexResponse(
            status="ok",
            primary_detail=req.sub_items[0].detail if req.sub_items else SubDetail(tag="none"),
        )

    @rpc
    async def quick_ping(self) -> str:
        """One-line summary ping."""
        return "pong"

    @rpc
    async def no_doc_op(self) -> int:
        return 42

    @validated_listener("events.complex.v1", EventPayload, durable="complex_processor")
    async def handle_event(self, message: EventPayload) -> None:
        """Handle incoming validated complex event.

        Updates downstream projections.
        """
        pass

    @listener("tasks.push", durable="push_worker")
    async def on_push_task(self, data: str) -> None:
        """Handle push durable task."""
        pass

    @listener("tasks.pull", durable="pull_worker", pull=True)
    async def on_pull_task(self, data: str) -> None:
        """Handle pull durable task."""
        pass

    @listener("alerts.all", fanout=True)
    async def on_broadcast_alert(self, msg: str) -> None:
        """Handle fanout broadcast alert."""
        pass

    @broadcast("system.heartbeat")
    async def on_heartbeat(self, beat: int) -> None:
        """Handle system broadcast heartbeat."""
        pass


# ---------------------------------------------------------------------------
# Unit Tests: Pydantic JSON Schemas in Components
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_components_catalog_contains_all_nested_and_return_models() -> None:
    desc = describe(ComprehensiveService, service="comp", version="1.0.0")

    # Assert components is a non-empty dictionary
    assert isinstance(desc.components, dict)
    titles = {s.get("title") for s in desc.components.values() if isinstance(s, dict)}

    # Parameter models and nested fields
    assert "ComplexRequest" in titles
    assert "SubItem" in titles
    assert "SubDetail" in titles
    # Return models
    assert "ComplexResponse" in titles
    # Listener models
    assert "EventPayload" in titles


@pytest.mark.unit
def test_component_keys_match_sha256_prefix_and_typerefs() -> None:
    desc = describe(ComprehensiveService, service="comp", version="1.0.0")

    for schema_hash, schema in desc.components.items():
        assert len(schema_hash) == 16
        expected_hash = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]
        assert schema_hash == expected_hash

    # Check method param type ref matches components
    m = desc.method("execute_task")
    assert m is not None
    req_param_hash = m.params[0].type.get("schema_hash")
    assert req_param_hash in desc.components
    assert desc.components[req_param_hash]["title"] == "ComplexRequest"

    # Check method return type ref matches components
    ret_hash = m.returns.get("schema_hash")
    assert ret_hash in desc.components
    assert desc.components[ret_hash]["title"] == "ComplexResponse"


@pytest.mark.unit
def test_cyclic_model_schema_collection_terminates() -> None:
    CyclicTree.model_rebuild()
    out = collect_model_schemas(CyclicTree)
    assert len(out) == 1
    collected_schema = next(iter(out.values()))
    assert "$ref" in collected_schema or "title" in collected_schema
    assert "CyclicTree" in str(collected_schema)


@pytest.mark.unit
def test_components_round_trip_serialization() -> None:
    desc = describe(ComprehensiveService, service="comp", version="1.0.0")
    d_dict = desc.to_dict()
    assert "components" in d_dict
    restored = Description.from_dict(d_dict)
    assert restored.components == desc.components
    assert canonical(restored.to_dict()) == canonical(d_dict)


# ---------------------------------------------------------------------------
# Unit Tests: Listeners and Streams Discovery
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_listeners_discovery_and_queue_group_semantics() -> None:
    desc = describe(ComprehensiveService, service="comp", version="1.0.0")
    assert len(desc.listeners) == 5

    # Validated push listener
    val_l = desc.listener("events.complex.v1")
    assert val_l is not None
    assert val_l.is_validated is True
    assert val_l.durable == "complex_processor"
    assert val_l.pull is False
    assert val_l.queue_group == "complex_processor"
    assert val_l.schema is not None
    assert val_l.schema.get("qualname") == "EventPayload"

    # Push durable listener
    push_l = desc.listener("tasks.push")
    assert push_l is not None
    assert push_l.durable == "push_worker"
    assert push_l.pull is False
    assert push_l.fanout is False
    assert push_l.queue_group == "push_worker"

    # Pull durable listener
    pull_l = desc.listener("tasks.pull")
    assert pull_l is not None
    assert pull_l.durable == "pull_worker"
    assert pull_l.pull is True
    assert pull_l.queue_group is None

    # Fanout listener
    fanout_l = desc.listener("alerts.all")
    assert fanout_l is not None
    assert fanout_l.fanout is True
    assert fanout_l.durable is None
    assert fanout_l.queue_group is None

    # Broadcast handler
    bcast_l = desc.listener("system.heartbeat")
    assert bcast_l is not None
    assert bcast_l.fanout is True
    assert bcast_l.is_broadcast is True
    assert bcast_l.durable is None
    assert bcast_l.queue_group is None


@pytest.mark.unit
def test_jetstream_stream_introspection_from_config() -> None:
    config = ServiceConfig(
        name="comp",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="STREAM_A", subjects=["events.a.*"], storage="file"),
            StreamSpec(
                name="STREAM_B",
                subjects=["events.b.*"],
                storage="memory",
                retention="interest",
                max_age_seconds=3600.0,
            ),
        ],
    )
    desc = describe(ComprehensiveService, config=config)
    assert len(desc.streams) == 2

    s_a = desc.stream("STREAM_A")
    assert s_a is not None
    assert s_a.subjects == ["events.a.*"]
    assert s_a.storage == "file"
    assert s_a.retention == "limits"

    s_b = desc.stream("STREAM_B")
    assert s_b is not None
    assert s_b.subjects == ["events.b.*"]
    assert s_b.storage == "memory"
    assert s_b.retention == "interest"
    assert s_b.max_age_seconds == 3600.0


@pytest.mark.unit
def test_listener_and_stream_round_trip_serialization() -> None:
    config = ServiceConfig(
        name="comp",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="S1", subjects=["s1.*"])],
    )
    desc = describe(ComprehensiveService, config=config)
    d_dict = desc.to_dict()
    assert "listeners" in d_dict
    assert "streams" in d_dict
    restored = Description.from_dict(d_dict)
    assert len(restored.listeners) == len(desc.listeners)
    assert len(restored.streams) == len(desc.streams)
    assert canonical(restored.to_dict()) == canonical(d_dict)


# ---------------------------------------------------------------------------
# Unit Tests: Docstring Preservation
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_handlerspec_preserves_summary_and_full_description() -> None:
    spec = build_handler_spec(
        "execute_task",
        ComprehensiveService.execute_task,
        owner=ComprehensiveService,
    )
    assert spec.doc == "Execute complex business task."
    assert spec.doc_summary == "Execute complex business task."
    assert spec.doc_description is not None
    assert "Detailed notes:" in spec.doc_description
    assert spec.description == spec.doc_description


@pytest.mark.unit
def test_method_docstring_fields_multiline_singleline_none() -> None:
    desc = describe(ComprehensiveService, service="comp", version="1.0.0")

    # Multi-line method
    m_multi = desc.method("execute_task")
    assert m_multi is not None
    assert m_multi.doc == "Execute complex business task."
    assert m_multi.doc_summary == "Execute complex business task."
    assert m_multi.description is not None
    assert "Detailed notes:" in m_multi.description
    assert "- Validates sub_items and detail recursively." in m_multi.description

    # Single-line method
    m_single = desc.method("quick_ping")
    assert m_single is not None
    assert m_single.doc == "One-line summary ping."
    assert m_single.doc_summary == "One-line summary ping."
    assert m_single.description == "One-line summary ping."

    # No docstring method
    m_none = desc.method("no_doc_op")
    assert m_none is not None
    assert m_none.doc is None
    assert m_none.doc_summary is None
    assert m_none.description is None


@pytest.mark.unit
def test_event_listener_docstrings_preserved() -> None:
    desc = describe(ComprehensiveService, service="comp", version="1.0.0")

    val_l = desc.listener("events.complex.v1")
    assert val_l is not None
    assert val_l.doc == "Handle incoming validated complex event."
    assert val_l.doc_summary == "Handle incoming validated complex event."
    assert val_l.description is not None
    assert "Updates downstream projections." in val_l.description


@pytest.mark.unit
def test_backward_compatibility_with_legacy_description_dict() -> None:
    legacy_dict = {
        "service": "legacy",
        "version": "0.1.0",
        "description_hash": "sha256:legacy",
        "methods": [
            {
                "name": "ping",
                "doc": "Ping doc",
                "params": [],
                "returns": {"kind": "scalar", "name": "str"},
                "signature_hash": "sha256:p",
            }
        ],
    }
    desc = Description.from_dict(legacy_dict)
    assert desc.service == "legacy"
    assert desc.components == {}
    assert desc.listeners == []
    assert desc.streams == []
    m = desc.methods[0]
    assert m.doc == "Ping doc"
    assert m.doc_summary is None
    assert m.description is None
