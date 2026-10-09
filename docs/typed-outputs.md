# Typed outbound bindings

A service template can declare a fixed event schema and choose its destination
from validated business settings. `Output` separates values accepted at
activation from values supplied for each publication. Neither changes the RPC
table, service class, extensions, broker credentials or inbound address.

```python
from pydantic import BaseModel, Field

from cliffracer import CliffracerService, Output, ServiceConfig, rpc
from cliffracer.runners import ServiceTemplate, TemplateCatalog


class BatchSettings(BaseModel):
    batch: str


class ShipmentProgress(BaseModel):
    quantity: int = Field(gt=0)


class Shipments(CliffracerService):
    progress = Output(
        ShipmentProgress,
        "batches.{batch}.orders.{order_id}.progress",
        settings=("batch",),
        parameters=("order_id",),
    )

    @rpc
    async def ship(self, order_id: str, quantity: int) -> int:
        await self.progress.publish(
            ShipmentProgress(quantity=quantity),
            parameters={"order_id": order_id},
        )
        return quantity


catalog = TemplateCatalog()
template = catalog.register(
    ServiceTemplate(
        name="shipments",
        revision="shipping-a",
        service_class=Shipments,
        settings_model=BatchSettings,
        factory=lambda settings, runtime: Shipments(runtime),
    )
)
accepted = template.normalize({"batch": "north"})
runtime = ServiceConfig(
    name="shipping_host",
    namespace="retail",
    subject_prefix="east",
    health_port=0,
    nats_inbox_prefix="_INBOX.shipping",
)
planned = template.bind_outputs(accepted, runtime)
assert planned.publish_subjects == (
    "east.retail.batches.north.orders.*.progress",
)
```

Register the same `ServiceTemplate` with `LocalSupervisor` for supervised
activation, or call `template.construct(accepted, runtime)` and manage the
service's lifecycle yourself. Ordinary service construction does not bind
these outputs; publication before binding raises `OutputError`.

## Substitution and validation

Every placeholder occupies one entire dot-separated subject token. Names are
simple identifiers beginning with a letter, declared in exactly one of
`settings` or `parameters`. Setting names refer to the model's Python field
names. Repeating a declared placeholder uses the same value.

Values must be nonempty strings without dots, whitespace, control characters,
wildcards or braces. Literal tokens follow the same restrictions. Attribute
access, indexing, partial-token substitution, filters, function calls, loops
and Jinja blocks are refused. There is no expression evaluator or access to
other settings. Namespace and environment prefix are applied once through
the ordinary subject builder.

Settings and route values are retained before activation is admitted. Matching
`ensure` retries fill omitted optional fields from the accepted settings,
including environment-derived defaults. Required fields remain required;
explicitly different values conflict. `reactivate` retains the accepted settings
and routes. Revalidation that changes an accepted value is refused. Caller or
factory mutation cannot redirect the retained binding.

Publication requires exactly the declared parameter names and the exact
declared Pydantic model class. The serialized payload must be a JSON object
that validates strictly and round-trips unchanged through that model. This
also checks models created with `model_construct` or mutated after validation.
Asymmetric serializers and incompatible validation/serialization aliases must
satisfy that round-trip contract. Invalid values fail before transport submission.

## Contracts and producer identity

`describe(Shipments)` and the live describe endpoint include `outputs`: each
name, subject expression, binding-time declarations, and validation and
serialization schemas. `template.output_contract.identity` fingerprints that
table separately from RPC signatures. Registration, construction, live readiness
and typed publication check it for drift. Generated clients verify RPC methods;
that check alone does not verify events.

`reference.outputs` and `service.output_bindings` expose immutable routes and
the output contract. `outputs.output("progress").subject` contains accepted
activation tokens and remaining publication placeholders; `.publish_subjects`
contains the corresponding concrete or single-token wildcard families.
Inspection includes public routing values rather than arbitrary settings or
broker credentials. Do not use secrets as subject tokens or logical identities.

Typed events retain the standard `data`, `timestamp`, `source_service` and
`correlation_id` envelope fields, and add:

```json
{
  "output": {
    "name": "progress",
    "contract": "sha256:<output-contract-digest>",
    "producer": {
      "scope": "retail",
      "template": "shipments",
      "key": "north",
      "revision": "shipping-a",
      "incarnation": "<supervisor-incarnation>",
      "generation": 1,
      "service": "<assigned-service-name>"
    }
  }
}
```

A consumer holding the current reference can call
`reference.outputs.accepts(envelope["output"])` to recognize that producer
generation and contract. Retained events from a predecessor do not match its
successor. This metadata is not authentication, distributed ownership or
side-effect fencing: an authorized publisher can forge it, and a consumer
must obtain its current reference from a trusted source. Direct catalog
construction emits `producer: null` and has no supervisor producer identity.

## Broker permissions and JetStream

Use the reviewed routes explicitly when deriving permissions:

```python
from cliffracer.broker_permissions import broker_permissions

policy = broker_permissions(
    Shipments,
    runtime,
    role="service",
    extra_publish=planned.publish_subjects,
)
```

The deployment must install the policy. Binding a subject does not grant broker
access or validate other publishers' payloads. Supervisor-assigned RPC addresses
require corresponding service-role grants; the example above uses a directly
constructed service's assigned configuration. Inbound listeners and dynamic
subscription changes remain outside the template contract.

With JetStream enabled, each concrete publication must be covered by the
service's declared streams, even if another application owns a stream that could
accept it. Outputs do not infer or provision streams. Stream subjects include
the namespace; `ServiceConfig` applies the environment prefix.

The ordinary serialization, send hooks, correlation headers, broker error and
acknowledgment behavior apply. `publish(..., idempotency_key="...")` supplies an
explicit key. Configured automatic deduplication includes the output contract
and producer metadata, so identical payloads from successor generations remain
distinct. Explicit and ambient keys remain caller-controlled and can deliberately
span generations.

See the [runnable shipment example](../examples/virtual_services/README.md),
[service templates](service-templates.md), [local supervisor](local-supervisor.md)
and [broker permissions](broker-permissions.md).
