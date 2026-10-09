"""Introspection primitives, read from a service's own registries.

These reach no transport: introspection answers from the handlers a service
has registered, so nothing here publishes or subscribes.

Covers Tiers 1-3:
- Tier 1: Feature verification for Pydantic component schema extraction, listener & stream describe discovery, full docstring preservation (>=5 per feature).
- Tier 2: Boundary & corner cases (complex nested models, optional/union fields, unannotated vs annotated listeners, multi-paragraph markdown docstrings).
- Tier 3: Introspection output validation against JSON Schema and the contract a service declares.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

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
from cliffracer.core.typed_rpc import build_handler_spec
from cliffracer.introspect import Description, describe

pytestmark = pytest.mark.unit


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
    """Verify Description extracts a populated components dictionary with valid schema hashes."""
    desc = _call_describe(CatalogService)
    assert len(desc.components) > 0, "Components dictionary must contain extracted schemas"
    assert all(
        isinstance(k, str) and len(k) == 16 and all(c in "0123456789abcdef" for c in k)
        for k in desc.components.keys()
    ), "All component keys must be 16-character hexadecimal schema hashes"
    assert all(isinstance(v, dict) and "title" in v for v in desc.components.values()), (
        "All component schemas must be valid schema dictionaries containing a title"
    )


def test_tier1_314_02_rpc_param_models_extracted_to_components() -> None:
    """Verify Pydantic models used in RPC parameters are collected in components."""
    desc = _call_describe(CatalogService)
    # Find OrderRequest schema
    schemas = desc.components.values()
    titles = [s.get("title") for s in schemas if isinstance(s, dict)]
    assert "OrderRequest" in titles, "OrderRequest schema must be present in components"


def test_tier1_314_03_rpc_return_models_extracted_to_components() -> None:
    """Verify Pydantic models used in RPC returns are collected in components."""
    desc = _call_describe(CatalogService)
    schemas = desc.components.values()
    titles = [s.get("title") for s in schemas if isinstance(s, dict)]
    assert "ReceiptResponse" in titles, "ReceiptResponse schema must be present in components"


def test_tier1_314_04_nested_pydantic_submodels_collected() -> None:
    """Verify nested Pydantic submodels (ItemModel inside OrderRequest) are cataloged."""
    desc = _call_describe(CatalogService)
    schemas = desc.components.values()
    titles = [s.get("title") for s in schemas if isinstance(s, dict)]
    assert "ItemModel" in titles, "Nested ItemModel schema must be collected in components"


def test_tier1_314_05_validated_listener_schemas_in_components() -> None:
    """Verify @validated_listener schemas are collected in components."""
    desc = _call_describe(CatalogService)
    schemas = desc.components.values()
    titles = [s.get("title") for s in schemas if isinstance(s, dict)]
    assert "InventoryEvent" in titles, "InventoryEvent schema must be present in components"


def test_tier1_314_06_components_serialization_round_trip() -> None:
    """Verify Description serialization preserves a populated components map."""
    desc = _call_describe(CatalogService)
    assert len(desc.components) > 0, "Prerequisite: components map must be populated"
    d_dict = desc.to_dict()
    assert "components" in d_dict
    restored = Description.from_dict(d_dict)
    assert restored.components == desc.components


# === Feature: Expand describe() to Listeners and JetStream Streams ===


def test_tier1_315_01_listeners_list_present_in_description() -> None:
    """Verify Description extracts all declared listeners for the service."""
    desc = _call_describe(CatalogService)
    discovered_patterns = {
        getattr(item, "pattern", item.get("pattern") if isinstance(item, dict) else None)
        for item in desc.listeners
    }
    assert discovered_patterns == {"alerts.cluster", "inventory.updated", "system.broadcast"}


def test_tier1_315_02_listener_discovery_durable_and_fanout() -> None:
    """Verify @listener methods are cataloged with patterns, durable, and fanout."""
    desc = _call_describe(CatalogService)
    patterns = [
        getattr(item, "pattern", item.get("pattern") if isinstance(item, dict) else None)
        for item in desc.listeners
    ]
    assert "alerts.cluster" in patterns


def test_tier1_315_03_validated_listener_discovery_with_schema_ref() -> None:
    """Verify @validated_listener is discovered with schema TypeRef dictionary."""
    desc = _call_describe(CatalogService)
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
    stream_names = [
        getattr(s, "name", s.get("name") if isinstance(s, dict) else None) for s in desc.streams
    ]
    assert "CATALOG_ORDERS" in stream_names
    assert "INVENTORY_STREAM" in stream_names


# === Feature: Preserve Full Docstrings in HandlerSpec and Method ===


def test_tier1_316_01_method_metadata_has_doc_summary_and_description() -> None:
    """Verify Method metadata distinguishes between short summary and detailed description."""
    desc = _call_describe(CatalogService)
    m = desc.method("create_order")
    assert m is not None
    assert m.doc_summary == "Create a new order and generate a receipt."
    assert m.description is not None
    assert m.description != m.doc_summary
    assert len(m.description) > len(m.doc_summary)
    assert "### Detailed Business Logic" in m.description


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


class QueueGroupService(CliffracerService):
    """Durable listeners that are also fanout or pull: the cases the queue_group rule is for."""

    @listener("qg.fanout", durable="qg_fanout_worker", fanout=True)
    async def on_fanout_durable(self, note: str) -> None:
        pass

    @listener("qg.pull", durable="qg_pull_worker", pull=True)
    async def on_pull_durable(self, note: str) -> None:
        pass

    @validated_listener("qg.validated", InventoryEvent, durable="qg_validated_worker", fanout=True)
    async def on_validated_fanout_durable(self, message: InventoryEvent) -> None:
        pass

    @listener("qg.push", durable="qg_push_worker")
    async def on_push_durable(self, note: str) -> None:
        pass


def test_tier2_315_03_a_durable_that_is_fanout_or_pull_has_no_queue_group() -> None:
    """The fanout and pull clauses of the rule, which a fanout listener with no durable never reaches.

    Fanout and pull subscriptions are made without a queue group even when they name a durable
    (the framework permits both together when JetStream is off), so describing one as
    queue-grouped would publish the opposite of what the subscription does.
    """
    desc = _call_describe(QueueGroupService)
    listeners = {item.pattern: item for item in desc.listeners}

    assert {p: (item.durable, item.queue_group) for p, item in listeners.items()} == {
        "qg.fanout": ("qg_fanout_worker", None),
        "qg.pull": ("qg_pull_worker", None),
        "qg.validated": ("qg_validated_worker", None),
        # The control: a plain push durable is the case that does get one.
        "qg.push": ("qg_push_worker", "qg_push_worker"),
    }
    assert (listeners["qg.fanout"].fanout, listeners["qg.pull"].pull) == (True, True)


def test_tier2_315_02_describe_without_config_defaults_streams_empty() -> None:
    """Verify streams defaults to empty without configuration and populates with declared streams."""
    desc_default = _call_describe(CatalogService)
    assert desc_default.streams == []

    config = ServiceConfig(
        name="catalog",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="CATALOG_ORDERS", subjects=["orders.>"]),
        ],
    )
    desc_configured = _call_describe(CatalogService, config=config)
    assert len(desc_configured.streams) == 1
    assert desc_configured.streams[0].name == "CATALOG_ORDERS"


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
    """Tier 3: the components are the models CatalogService names, each a JSON Schema object."""
    desc = _call_describe(CatalogService)

    # Pinned before iterating: an empty map has nothing to be wrong about, so a collector that
    # found no models would otherwise read as conformance.
    assert {s["title"] for s in desc.components.values()} == {
        "InventoryEvent",
        "ItemModel",
        "OrderRequest",
        "ReceiptResponse",
    }
    for hash_key, schema in desc.components.items():
        assert isinstance(hash_key, str)
        assert len(hash_key) == 16, "schema_hash must be a 16-character hex string"
        # These are all models, so each is an object schema with its fields; a `title` alone
        # (which any pydantic schema carries) says nothing about that.
        assert schema["type"] == "object", schema["title"]
        assert schema["properties"], schema["title"]


def _assert_the_description_publishes_the_contract_of_catalog_service(d: dict[str, Any]) -> None:
    """The expectations are written from `CatalogService`'s own declarations above, not read back
    out of the description: a describe reply that published the RPC methods and nothing else, or
    dropped the schemas the methods point at, fails here."""
    assert d["service"] == "catalog" and d["version"] == "1.0.0"
    assert d["description_hash"].startswith("sha256:")

    assert {m["name"] for m in d["methods"]} == {
        "get_item",
        "create_order",
        "simple_ping",
        "unadorned_method",
    }

    # Three listeners, each with the subject it listens on, its handler and its fanout flag
    assert {(e["pattern"], e["handler_name"], e["fanout"]) for e in d["listeners"]} == {
        ("inventory.updated", "on_inventory_updated", False),
        ("alerts.cluster", "on_cluster_alert", True),
        ("system.broadcast", "on_system_broadcast", True),
    }
    assert len(d["listeners"]) == 3

    # The schemas: every model a method or listener names, once each, titled by its class
    assert sorted(schema["title"] for schema in d["components"].values()) == [
        "InventoryEvent",
        "ItemModel",
        "OrderRequest",
        "ReceiptResponse",
    ]

    # Every schema a method or listener points at is one of those published schemas
    referenced = {
        ref["schema_hash"]
        for m in d["methods"]
        for ref in (m["returns"], *(param["type"] for param in m["params"]))
        if ref.get("kind") == "model"
    } | {e["schema"]["schema_hash"] for e in d["listeners"] if e["schema"]}
    assert referenced == set(d["components"]), (referenced, set(d["components"]))


def test_tier3_the_description_publishes_the_contract_the_service_declares() -> None:
    """Tier 3: the published description carries every method, listener and schema declared.

    Nothing in this repository derives an AsyncAPI document from a description, so this reads the
    contract the description itself makes.
    """
    _assert_the_description_publishes_the_contract_of_catalog_service(
        _call_describe(CatalogService).to_dict()
    )


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: {**d, "listeners": []},
        lambda d: {**d, "components": {}},
        lambda d: {**d, "listeners": d["listeners"][:2]},
        lambda d: {**d, "methods": d["methods"][:1]},
        lambda d: {**d, "listeners": [{**e, "fanout": not e["fanout"]} for e in d["listeners"]]},
        lambda d: {**d, "components": dict(list(d["components"].items())[:-1])},
    ],
    ids=[
        "no-listeners",
        "no-components",
        "a-listener-lost",
        "methods-lost",
        "fanout-flags-flipped",
        "a-schema-lost",
    ],
)
def test_CONTROL_a_damaged_description_fails_the_contract(damage) -> None:
    """The contract check above is not vacuous: each loss it names makes it fail."""
    full = _call_describe(CatalogService).to_dict()

    with pytest.raises(AssertionError):
        _assert_the_description_publishes_the_contract_of_catalog_service(damage(full))


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

    # The legacy `doc` attribute still carries the handler's docstring. Asserting
    # it is present cannot fail: `doc` is a declared field on a frozen dataclass,
    # so it exists whatever `describe()` puts there. Reading the value on a
    # documented and an undocumented handler is what a legacy consumer depends
    # on, and it fails in both directions.
    documented = desc.method("simple_ping")
    assert documented is not None
    assert documented.doc == "Single line ping docstring.", (
        f"simple_ping is documented but describe() gave doc={documented.doc!r}; "
        "the handler's docstring is what the legacy attribute carries"
    )

    undocumented = desc.method("unadorned_method")
    assert undocumented is not None
    assert undocumented.doc is None, (
        f"unadorned_method has no docstring but describe() gave "
        f"doc={undocumented.doc!r}; absence reports None, not a placeholder"
    )
