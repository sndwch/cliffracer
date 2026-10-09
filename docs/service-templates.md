# Service template contracts

`ServiceTemplate` and `TemplateCatalog` live in `cliffracer.runners`. A template
registers a concrete service class, a typed settings model, a synchronous
factory, an immutable application revision, finite lifecycle budgets and
dispatch limits. Registration performs no broker operations.

These are construction and contract primitives. The
[local supervisor](local-supervisor.md) supplies activation scheduling, owner
lifetimes and bounded teardown. Applications using the catalog directly own
the constructed services' startup and cleanup.

## Register and construct

```python
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.runners import ServiceTemplate, TemplateCatalog


class ShipmentSettings(BaseModel):
    warehouse: str


class Shipments(CliffracerService):
    warehouse: str

    @rpc
    async def destination(self) -> str:
        return self.warehouse


def make_shipments(settings: ShipmentSettings, runtime: ServiceConfig) -> Shipments:
    service = Shipments(runtime)
    service.warehouse = settings.warehouse
    return service


catalog = TemplateCatalog()
template = catalog.register(
    ServiceTemplate(
        name="shipments",
        revision="warehouse-a",
        service_class=Shipments,
        settings_model=ShipmentSettings,
        factory=make_shipments,
        startup_timeout=10,
        cleanup_timeout=5,
        max_rpc_concurrency=8,
        max_async_rpc_concurrency=8,
    )
)
accepted = template.normalize({"warehouse": "north"})
child = template.construct(
    accepted,
    ServiceConfig(name="shipment_batch_a", namespace="retail", health_port=0),
)
```

The catalog resolves `(name, revision)` with `resolve(name, revision)`. Repeating
an identical registration returns the registered entry. A different declaration
under an existing revision raises `ActivationConflict`; a distinct revision can
coexist. Template revisions are application identifiers, independent of the
Cliffracer package version.

`normalize` validates a copy of the input and retains canonical JSON. Settings
must round-trip through their model's JSON representation without changing
values. Lossy serializers, including a redacted secret value that cannot be
restored, are refused. Each `materialize()` call produces a separately validated
model. Factory and caller mutations cannot alter accepted settings. Broker
credentials belong in host runtime configuration.

Each settings field must serialize under a key its model accepts as input,
including fields on nested models and dataclasses. Excluded fields, serializers
that omit fields, and aliases that cannot
read their serialized keys raise `TemplateError` before settings are accepted.
An `AliasPath` can participate in `AliasChoices` alongside the serialized key,
or the model can enable field-name population when it serializes by field name.
Retries retain omitted optional values with their validated Python types;
required fields remain required and explicit values are validated normally.

When a key is not one the model's schema reads, `normalize` asks pydantic before
it refuses: it validates the document again with a different value under each key
of the object that holds the field in turn, and accepts the field under a key only
when three things hold. Changing that key changes the field and no other field of
the object; the changed document dumps back with the change at that key and at no
other key of the object; and the field set alone to its new value on the original
model dumps with the change at that key and at no other. A stdlib dataclass that a
model uses three or more times is read through one shared definition, so a use
under another config than the schema shows is accepted this way. A validator that
fills a field from another field's key, that swaps or rotates keys between fields,
or that reads it from an extra key is refused: the field is written under its own
key, not the one that was changed, whether a serialization alias, a computed field
or a serializer writes that key. The field is also refused when no key is read,
when no different valid value can be built for it (a field with a single allowed
value, or one with no scalar under its key), when its value sits in a set, or when
the check has spent its budget. The budget is 256 units, plus 8 for each scalar the
document holds, and at most 4096; each copy of the document costs one unit, and
each validation, and each dump of the field set alone, one plus one for every
4096 bytes of the document's JSON, since it reads all of them. The check stops as
soon as the budget is spent, so the bytes it reads are bounded whatever the size of
the document. A large document is therefore refused as it is without this check: a
shared dataclass of 200 fields used four times is accepted, one of 300 or 600
fields is refused, and so is a 30-field one beside a 1 MB string or a 100,000-item
list. A field that is not set on construction (`init=False` or an `InitVar`) is
refused without being probed.

`construct` copies the host's `ServiceConfig`, applies the template's RPC and
async RPC concurrency limits, and passes that copy to the factory. It verifies
the exact returned class, the assigned configuration and the registered RPC
shape. Construction is synchronous and starts no resources; the lifecycle
budgets are metadata for the host enforcing them. Resource acquisition belongs
in service startup. Factories and lifecycle hooks are trusted application code.
The host's `on_connect`, `on_disconnect` and `on_error` callbacks retain their
original callable targets while mutable runtime data is copied.

Every successfully constructed object is claimed once across catalogs in the
process. An already started, stopped or claimed object is refused without
calling its teardown. Claims retain weak references, so they do not keep
otherwise unreachable services alive. A fresh object is required for another
activation.

## Supported declarations

Templates support RPC handlers, [typed outbound bindings](typed-outputs.md) and
service lifecycle hooks. Registration names
and refuses declared listeners, broadcasts and timers.
Worker-hook extensions can compose with the service; extensions overriding
`start` or `stop` require a broader resource-ownership
contract and are refused. This check also applies to extensions added by a
factory. Ordinary service construction outside the catalog retains its own
extension contract.

## Contracts and activation values

`cliffracer.runners.contracts` provides `RpcContract`, `LogicalIdentity`,
`ActivationAddress`, `ActivationReference`, `ActivationSnapshot`,
`ActivationState` and `CleanupOutcome`.

An `RpcContract` freezes the complete sorted method/signature table from
`describe(service_class)`. Existing canonical method signatures include model
schema hashes in their type references: a parameter model is hashed by the JSON Schema of what
a caller may send, and a return model by the JSON Schema of what the handler writes, so a
`computed_field` or a `serialization_alias` on a return model changes the contract.
`verify(description)` reports changed,
missing and extra methods through `ContractMismatch`. Documentation metadata
and instance names do not change contract identity. Construction refuses a
class whose RPC shape, or a settings model whose schema, differs from the
registered one. Registering an existing revision after such a change raises
`ActivationConflict`; the changed declaration needs a new revision.

An activation reference contains scope/template/key identity, supervisor
incarnation, generation, revision, RPC contract, output bindings and address. The address contains
the service name, namespace and subject prefix; it carries no broker credentials.
These immutable values describe a particular activation. They neither start a
service nor grant authorization or distributed ownership.

Generate a client from the template class using the ordinary client generator.
`reference.bind(GeneratedClient, nc=connection, timeout=5)` checks the complete
client signature table and creates that client at the reference's address.
Connection and request options are ordinary `ServiceClient` constructor
arguments. Routing overrides are refused. The generated client's ordinary
live verification still runs on first use; binding alone proves no readiness.

The reference pins all routing components, including an explicitly absent
namespace or prefix. For ordinary clients, `ServiceClient(subject_prefix=...)`
selects a prefix, and `subject_prefix=""` selects an unprefixed address. Omitting
the option, or passing `None`, reads `CLIFFRACER_SUBJECT_PREFIX` as the default.

`SupervisionError` is the common error base. `TemplateError` describes invalid
declarations or construction results; `ContractMismatch` specializes it.
`ActivationConflict`, `ActivationCapacityError` and `ActivationUnavailable`
describe rejected activation operations. Snapshot values contain lifecycle
observations and cleanup outcomes; applications retain business settings and
credentials separately.
