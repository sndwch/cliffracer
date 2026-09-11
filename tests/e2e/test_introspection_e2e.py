"""End-to-End E2E Test Suite for Introspection Primitives.

Covers Tiers 1-3:
- Tier 1: Feature verification for Pydantic component schema extraction, listener & stream describe discovery, full docstring preservation (>=5 per feature).
- Tier 2: Boundary & corner cases (complex nested models, optional/union fields, unannotated vs annotated listeners, multi-paragraph markdown docstrings).
- Tier 3: Introspection output validation against JSON Schema and AsyncAPI 3.0 requirements.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

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
from cliffracer.core.typed_rpc import build_handler_spec
from cliffracer.introspect import Description, describe

try:
    from cliffracer.introspect import EventListenerDescription, StreamDescription

    HAS_LISTENER_STREAM_DESC = True
except ImportError:
    EventListenerDescription = None  # type: ignore[assignment, misc]
    StreamDescription = None  # type: ignore[assignment, misc]
    HAS_LISTENER_STREAM_DESC = False


# ---------------------------------------------------------------------------
# Test Domain Models for Introspection
# ---------------------------------------------------------------------------
class ItemModel(BaseModel):
    """An individual line item within an order."""

    sku: str = Field(description="Stock keeping unit identifier")
    quantity: int = Field(default=1, ge=1, description="Quantity of items")
    unit_price: float = Field(gt=0, description="Unit price in USD")


class OrderRequest(BaseModel):
    """An order creation request containing multiple line items."""

    order_id: str
    items: list[ItemModel]
    notes: str | None = None


class ReceiptResponse(BaseModel):
    """Receipt emitted after successful order placement."""

    order_id: str
    total_amount: float
    status: str = "confirmed"


class InventoryEvent(BaseModel):
    """Event emitted when inventory levels adjust."""

    sku: str
    new_quantity: int


class RecursiveNode(BaseModel):
    """Tree node structure testing recursive schema handling."""

    node_id: str
    children: list[RecursiveNode] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Sample Services with Rich Type Signatures & Docstrings
# ---------------------------------------------------------------------------
class CatalogService(CliffracerService):
    """Catalog service managing store items and inventory."""

    @rpc
    async def get_item(self, sku: str) -> ItemModel:
        """Fetch item details by SKU.

        Queries the primary catalog store for item details.

        Returns:
            ItemModel containing sku, quantity, and unit_price.
        """
        return ItemModel(sku=sku, quantity=10, unit_price=19.99)

    @rpc
    async def create_order(self, order: OrderRequest) -> ReceiptResponse:
        """Create a new order and generate a receipt.

        ### Detailed Business Logic
        1. Validates SKU availability across distributed warehouses.
        2. Deducts quantity from inventory.
        3. Generates cryptographic receipt and returns confirmation.

        Note:
            Orders with zero items are refused during schema validation.
        """
        total = sum(item.quantity * item.unit_price for item in order.items)
        return ReceiptResponse(order_id=order.order_id, total_amount=total)

    @rpc
    async def simple_ping(self) -> str:
        """Single line ping docstring."""
        return "pong"

    @rpc
    async def unadorned_method(self) -> str:
        return "no doc"

    @validated_listener("inventory.updated", InventoryEvent, durable="inventory_sync")
    async def on_inventory_updated(self, message: InventoryEvent) -> None:
        """Consume inventory updates from JetStream stream.

        Maintains in-memory catalog cache consistency across cluster nodes.
        """
        pass

    @listener("alerts.cluster", fanout=True)
    async def on_cluster_alert(self, alert_type: str, severity: str) -> None:
        """Handle cluster-wide broadcast alerts."""
        pass

    @broadcast("system.broadcast")
    async def on_system_broadcast(self, msg: str) -> None:
        """Fanout broadcast handler."""
        pass


def _call_describe(cls: type, **kwargs: Any) -> Description:
    """Helper to call describe() adapting to expanded or legacy signature."""
    sig = inspect.signature(describe)
    call_kwargs: dict[str, Any] = {"service": "catalog", "version": "1.0.0"}
    if "config" in sig.parameters and "config" in kwargs:
        call_kwargs["config"] = kwargs["config"]
    return describe(cls, **call_kwargs)


# ---------------------------------------------------------------------------
# Tier 1: Feature Coverage (>=5 tests per feature)
# ---------------------------------------------------------------------------

# === Feature: Pydantic Component Schema Extraction ===


def test_tier1_314_01_components_dict_present_in_description() -> None:
    """Verify Description includes top-level components dictionary."""
    desc = _call_describe(CatalogService)
    assert hasattr(desc, "components"), "Description must have 'components' field"
    assert isinstance(desc.components, dict)


def test_tier1_314_02_rpc_param_models_extracted_to_components() -> None:
    """Verify Pydantic models used in RPC parameters are collected in components."""
    desc = _call_describe(CatalogService)
    assert hasattr(desc, "components"), "Description missing components field"
    # Find OrderRequest schema
    schemas = desc.components.values()
    titles = [s.get("title") for s in schemas if isinstance(s, dict)]
    assert "OrderRequest" in titles, "OrderRequest schema must be present in components"


def test_tier1_314_03_rpc_return_models_extracted_to_components() -> None:
    """Verify Pydantic models used in RPC returns are collected in components."""
    desc = _call_describe(CatalogService)
    assert hasattr(desc, "components"), "Description missing components field"
    schemas = desc.components.values()
    titles = [s.get("title") for s in schemas if isinstance(s, dict)]
    assert "ReceiptResponse" in titles, "ReceiptResponse schema must be present in components"


def test_tier1_314_04_nested_pydantic_submodels_collected() -> None:
    """Verify nested Pydantic submodels (ItemModel inside OrderRequest) are cataloged."""
    desc = _call_describe(CatalogService)
    assert hasattr(desc, "components"), "Description missing components field"
    schemas = desc.components.values()
    titles = [s.get("title") for s in schemas if isinstance(s, dict)]
    assert "ItemModel" in titles, "Nested ItemModel schema must be collected in components"


def test_tier1_314_05_validated_listener_schemas_in_components() -> None:
    """Verify @validated_listener schemas are collected in components."""
    desc = _call_describe(CatalogService)
    assert hasattr(desc, "components"), "Description missing components field"
    schemas = desc.components.values()
    titles = [s.get("title") for s in schemas if isinstance(s, dict)]
    assert "InventoryEvent" in titles, "InventoryEvent schema must be present in components"


def test_tier1_314_06_components_serialization_round_trip() -> None:
    """Verify Description.to_dict() and from_dict() preserve components map."""
    desc = _call_describe(CatalogService)
    d_dict = desc.to_dict()
    assert "components" in d_dict
    restored = Description.from_dict(d_dict)
    assert restored.components == desc.components


# === Feature: Expand describe() to Listeners and JetStream Streams ===


def test_tier1_315_01_listeners_list_present_in_description() -> None:
    """Verify Description includes listeners list."""
    desc = _call_describe(CatalogService)
    assert hasattr(desc, "listeners"), "Description must have 'listeners' field"
    assert isinstance(desc.listeners, list)


def test_tier1_315_02_listener_discovery_durable_and_fanout() -> None:
    """Verify @listener methods are cataloged with patterns, durable, and fanout."""
    desc = _call_describe(CatalogService)
    assert hasattr(desc, "listeners"), "Description missing listeners field"
    patterns = [
        getattr(item, "pattern", item.get("pattern") if isinstance(item, dict) else None)
        for item in desc.listeners
    ]
    assert "alerts.cluster" in patterns


def test_tier1_315_03_validated_listener_discovery_with_schema_ref() -> None:
    """Verify @validated_listener is discovered with schema TypeRef dictionary."""
    desc = _call_describe(CatalogService)
    assert hasattr(desc, "listeners"), "Description missing listeners field"
    val_listener = next(
        (item for item in desc.listeners if getattr(item, "pattern", None) == "inventory.updated"),
        None,
    )
    assert val_listener is not None
    assert val_listener.schema is not None
    assert val_listener.schema.get("kind") == "model"
    assert val_listener.schema.get("qualname") == "InventoryEvent"


def test_tier1_315_04_broadcast_handler_discovery() -> None:
    """Verify @broadcast handler is discovered with fanout=True."""
    desc = _call_describe(CatalogService)
    assert hasattr(desc, "listeners"), "Description missing listeners field"
    b_listener = next(
        (item for item in desc.listeners if getattr(item, "pattern", None) == "system.broadcast"),
        None,
    )
    assert b_listener is not None
    assert b_listener.fanout is True


def test_tier1_315_05_jetstream_stream_discovery_from_config() -> None:
    """Verify declared JetStream streams from ServiceConfig are cataloged in Description.streams."""
    config = ServiceConfig(
        name="catalog",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="CATALOG_ORDERS", subjects=["orders.>"]),
            StreamSpec(name="INVENTORY_STREAM", subjects=["inventory.*"], storage="memory"),
        ],
    )
    desc = _call_describe(CatalogService, config=config)
    assert hasattr(desc, "streams"), "Description must have 'streams' field"
    stream_names = [
        getattr(s, "name", s.get("name") if isinstance(s, dict) else None) for s in desc.streams
    ]
    assert "CATALOG_ORDERS" in stream_names
    assert "INVENTORY_STREAM" in stream_names


# === Feature: Preserve Full Docstrings in HandlerSpec and Method ===


def test_tier1_316_01_method_metadata_has_doc_summary_and_description() -> None:
    """Verify Method dataclass has doc_summary and description fields."""
    desc = _call_describe(CatalogService)
    m = desc.method("create_order")
    assert m is not None
    assert hasattr(m, "doc_summary"), "Method must have doc_summary"
    assert hasattr(m, "description"), "Method must have description"


def test_tier1_316_02_single_line_docstring_preserved() -> None:
    """Verify single line docstring is preserved across doc, doc_summary, and description."""
    desc = _call_describe(CatalogService)
    m = desc.method("simple_ping")
    assert m is not None
    assert m.doc == "Single line ping docstring."
    assert m.doc_summary == "Single line ping docstring."
    assert m.description == "Single line ping docstring."


def test_tier1_316_03_multiline_docstring_full_text_preserved() -> None:
    """Verify multi-line Markdown docstring is preserved with all paragraphs in description."""
    desc = _call_describe(CatalogService)
    m = desc.method("create_order")
    assert m is not None
    assert m.doc_summary == "Create a new order and generate a receipt."
    assert m.description is not None
    assert "### Detailed Business Logic" in m.description
    assert "1. Validates SKU availability" in m.description
    assert "cryptographic receipt" in m.description


def test_tier1_316_04_handlerspec_preserves_full_docstring() -> None:
    """Verify build_handler_spec preserves doc_description and doc_summary."""
    spec = build_handler_spec("create_order", CatalogService.create_order, owner=CatalogService)
    assert spec.doc_summary == "Create a new order and generate a receipt."
    assert spec.doc_description is not None
    assert "### Detailed Business Logic" in spec.doc_description


def test_tier1_316_05_unadorned_method_sets_doc_fields_none() -> None:
    """Verify method without docstring safely sets doc, doc_summary, and description to None."""
    desc = _call_describe(CatalogService)
    m = desc.method("unadorned_method")
    assert m is not None
    assert m.doc is None
    assert m.doc_summary is None
    assert m.description is None


# ---------------------------------------------------------------------------
# Tier 2: Boundary & Corner Cases
# ---------------------------------------------------------------------------


class ComplexTypesService(CliffracerService):
    """Service testing boundary type annotations."""

    @rpc
    async def process_optional(self, item: ItemModel | None) -> ItemModel | None:
        return item

    @rpc
    async def process_annotated(
        self,
        count: Annotated[int, Field(ge=1, le=50)],
    ) -> int:
        return count

    @rpc
    async def process_dict_mapping(
        self,
        mapping: dict[str, ItemModel],
    ) -> dict[str, ItemModel]:
        return mapping


def test_tier2_314_01_complex_generic_types_in_components() -> None:
    """Boundary: Models wrapped in Optional and dict are extracted into components."""
    desc = _call_describe(ComplexTypesService)
    assert hasattr(desc, "components")
    schemas = desc.components.values()
    titles = [s.get("title") for s in schemas if isinstance(s, dict)]
    assert "ItemModel" in titles


def test_tier2_314_02_duplicate_model_references_deduplicated() -> None:
    """Boundary: Multiple methods referencing the same model do not duplicate schema entries."""
    desc = _call_describe(ComplexTypesService)
    # Count how many schemas have title == 'ItemModel'
    item_schemas = [
        s for s in desc.components.values() if isinstance(s, dict) and s.get("title") == "ItemModel"
    ]
    assert len(item_schemas) == 1, "Components dictionary must deduplicate schemas by schema_hash"


def test_tier2_315_01_queue_group_invariant_for_push_vs_fanout() -> None:
    """Boundary: queue_group == durable for push durable consumers; None for fanout/broadcast."""
    desc = _call_describe(CatalogService)
    val_listener = next(
        item for item in desc.listeners if getattr(item, "pattern", None) == "inventory.updated"
    )
    assert val_listener.queue_group == "inventory_sync"

    fanout_listener = next(
        item for item in desc.listeners if getattr(item, "pattern", None) == "alerts.cluster"
    )
    assert fanout_listener.queue_group is None


def test_tier2_315_02_describe_without_config_defaults_streams_empty() -> None:
    """Boundary: describe(cls) without config defaults streams to empty list []."""
    desc = _call_describe(CatalogService)
    assert hasattr(desc, "streams")
    assert desc.streams == []


def test_tier2_316_01_multiparagraph_markdown_indentation_normalized() -> None:
    """Boundary: Docstring leading whitespace normalized without corrupting code blocks."""
    desc = _call_describe(CatalogService)
    m = desc.method("create_order")
    assert m is not None and m.description is not None
    # Verify no redundant leading space on the first paragraph line
    lines = m.description.splitlines()
    assert lines[0] == "Create a new order and generate a receipt."


# ---------------------------------------------------------------------------
# Tier 3: Specification Conformance & Validation
# ---------------------------------------------------------------------------


def test_tier3_components_schema_conforms_to_json_schema() -> None:
    """Tier 3: All schemas in components contain valid JSON Schema object structure."""
    desc = _call_describe(CatalogService)
    for hash_key, schema in desc.components.items():
        assert isinstance(hash_key, str)
        assert len(hash_key) == 16, "schema_hash must be a 16-character hex string"
        assert isinstance(schema, dict)
        assert "type" in schema or "$ref" in schema or "properties" in schema or "title" in schema


def test_tier3_asyncapi_3_compatibility() -> None:
    """Tier 3: Verify Description.to_dict() provides complete AsyncAPI 3.0 elements."""
    desc = _call_describe(CatalogService)
    d = desc.to_dict()

    # Operations & Channels mapped from methods and listeners
    assert "methods" in d
    assert "listeners" in d
    assert "components" in d

    # Each listener maps to an AsyncAPI channel and operation
    for listener_entry in d["listeners"]:
        assert "pattern" in listener_entry
        assert "handler_name" in listener_entry
        assert "fanout" in listener_entry

    # Each component maps to AsyncAPI components.schemas
    for _h, s in d["components"].items():
        assert "title" in s


def test_tier3_deterministic_description_hash() -> None:
    """Tier 3: Canonical description hash is deterministic across multiple evaluations."""
    desc1 = _call_describe(CatalogService)
    desc2 = _call_describe(CatalogService)
    assert desc1.description_hash == desc2.description_hash
    assert desc1.description_hash.startswith("sha256:")


def test_tier3_legacy_client_backwards_compatibility() -> None:
    """Tier 3: Legacy consumers reading only method and param properties encounter no regression."""
    desc = _call_describe(CatalogService)
    for m in desc.methods:
        assert isinstance(m.name, str)
        assert isinstance(m.params, list)
        assert isinstance(m.returns, dict)
        assert isinstance(m.signature_hash, str)
        # Old 'doc' attribute remains present
        assert hasattr(m, "doc")
