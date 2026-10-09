# Order parents and shipment workers

Run from the repository root with a reachable NATS broker:

```bash
uv run python -m examples.virtual_services.orders
```

The default broker comes from `ServiceConfig`. Set `CLIFFRACER_NATS_URL` for this
example to use another address. Health listeners use ephemeral loopback ports.
Each invocation chooses its own broker namespace, including progress traffic.

Two order services start before any shipment child exists. A real parent RPC
requests a shipment worker through `ServiceOwner`, then calls the worker using
the checked-in `ShipmentsClient`. Repeating a batch key reuses its state.
Different keys get different instances. Changing a batch's warehouse returns
`conflict`; exhausting the two-child limit returns `capacity` with a next step.

The example stops the north parent and sends another real shipment request to
the south parent. North's child is stopped, while south's total continues from
4 to 5. It prints these observed results and both parents' cleanup outcomes as
JSON. A progress subscriber receives four shipment updates.

The `batch` setting binds each child's declared `progress` output to
`shipments.progress.{batch}`. Its payload is the fixed `Receipt` model. Accepted
tokens are retained independently of factory settings; publication checks the
schema and applies the host namespace and subject prefix once. Each event also
carries the output contract and supervisor generation. See
[typed outbound bindings](../../docs/typed-outputs.md) for per-publication
parameters, broker grants and stale-event recognition.

## Client generation

The client is generated from the class before any worker is activated:

```bash
PYTHONPATH=. uv run cliffracer-generate-client \
  --class examples.virtual_services.shipments:Shipments --service shipments --namespace retail \
  --out examples/virtual_services/shipment_client.py --check
```

Omit `--check` to regenerate. The directory's Ruff configuration matches the
generator's standard formatting, so the checked-in client stays reproducible.

## Lifetimes

The application starts and closes `LocalSupervisor`. Each bound `ServiceOwner`
opens a separate owner during parent setup. `SharedDependency(supervisor)`
shares the supervisor explicitly; the framework refuses to clone it. Parent
teardown closes that parent's children and descendants and retains the result
in `parent.children.cleanup_report`.

A parent-owned child does not outlive a cleanly stopped parent. To request a
child whose lifetime belongs to the host, explicitly open
`await supervisor.supervisor_owner(scope)` and use that handle directly.
Both choices still run in the same Python process. Process failure ends every
local activation; recovery and remote placement are separate capabilities.
The host keeps the event loop running; children cannot outlive its entry point.

See [the local supervisor reference](../../docs/local-supervisor.md) for finite
limits, retention, reactivation and unfinished-cleanup behavior.
