# Local service supervision

Templates can publish [typed outputs](typed-outputs.md) on routes retained from
accepted activation settings. References expose those routes and producer
generation metadata alongside the RPC address.

`LocalSupervisor` in `cliffracer.runners` owns dynamically requested services on
the application's event loop. Register a [service template](service-templates.md),
open an owner, and request an activation. The returned reference binds an
ordinary generated client. The supervisor never installs process signal
handlers or replays a business RPC.

## Host wiring

This function accepts a registered application template and its generated client
class. The application's async entry point awaits it with the broker configuration
it normally supplies to its services.

```python
from cliffracer import ServiceConfig
from cliffracer.runners import LocalSupervisor, SupervisorLimits


async def ship_batch(template, shipment_client, parcel):
    runtime = ServiceConfig(name="shipping_host", health_host="127.0.0.1", health_port=0)
    limits = SupervisorLimits(
        max_active=8,
        max_records=64,
        max_owners=16,
        max_depth=4,
        startup_timeout=10,
        cleanup_timeout=5,
        wait_timeout=20,
        retention=300,
    )
    async with LocalSupervisor(runtime, limits=limits) as supervisor:
        supervisor.register(template)
        owner = await supervisor.open_owner("retail")
        reference = await supervisor.ensure(
            owner,
            template.name,
            "batch-a",
            {"warehouse": "north", "destinations": ["retail"]},
            revision=template.revision,
        )
        async with reference.bind(shipment_client, nats_url=runtime.nats_url) as client:
            receipt = await client.ship(parcel)
        report = await supervisor.close_owner(owner)
        return receipt, report
```

When credentials or custom connection policy apply, bind the generated client
with an appropriately authenticated caller connection using `nc=connection`.
Activation references contain routing, not credentials.

Children receive copied host runtime data, a unique activation name and an
ephemeral health port. Host callbacks retain their original callable targets.
Template dispatch limits apply to each child. Startup and cleanup use the smaller
of the host and template budgets. A child's stop runs its phases in sequence: the
drain of active tasks, the cancellation grace, and the drain of its broker
connection. Each phase is given a fifth of the cleanup budget, so a child that
runs out every phase has closed inside the budget, with room for the work between
them. Raise `cleanup_timeout`
when a handler needs longer than that to stop.

`start()` and `close()` are available for explicit async lifetime wiring.
`async with` awaits close but does not turn incomplete cleanup into success.
Use explicit `close()` when the caller needs its returned `CleanupReport`.

## Parent service integration

`ServiceOwner`, exported from `cliffracer.runners`, is an extension for an
ordinary parent service. Declare it with `SharedDependency(supervisor)` so
each parent shares the host while receiving a separate owner handle:

```python
from cliffracer import CliffracerService
from cliffracer.core.extension import SharedDependency
from cliffracer.runners import ServiceOwner


def orders_class(supervisor):
    class Orders(CliffracerService):
        children = ServiceOwner(SharedDependency(supervisor), scope="retail")

    return Orders
```

From a parent handler, `await self.children.ensure(...)` takes the same
template, key, settings and revision arguments as the supervisor, with the
owner supplied by the extension. `reactivate(reference)` also uses that owner.
The adapter refuses new admission once parent shutdown has been requested.
Accepted parent handlers follow the ordinary drain policy; extension teardown
then closes the owner and its descendants. Failed or cancelled parent startup
also runs owner cleanup.

`parent.children.owner` exposes the issued handle after setup.
`parent.children.cleanup_report` retains the close result, including unfinished
work. Cancellation of the parent's cleanup waiter does not cancel accepted
owner cleanup; the adapter retains its task and later report. Stop parent
services before closing the host supervisor. Closing the host alone also
closes every remaining owner.

An owner records an explicit lifecycle, not Python garbage collection. Dropping
an application reference to a parent is not a shutdown operation. Parent-owned
children stop with the parent; explicitly supervisor-owned children can outlive
that requesting parent, but still share the host's process and failure boundary.
The application must keep its event loop running. Registry references and
background tasks do not keep a program alive after its host entry point returns.

The [runnable order/shipment example](../examples/virtual_services/README.md)
uses two parents, a pregenerated typed client and runtime progress channels.
Templates retain their RPC-only extension surface; this adapter belongs on
the ordinary parent service.

## Ownership and operations

An owner issued by `open_owner(scope, parent=...)` can contain descendant owners.
Close it when its application's lifetime ends. `supervisor_owner(scope)` explicitly
chooses an owner whose lifetime ends with host shutdown. Closing any owner first
closes admission for the whole subtree and then stops its children concurrently.
The supervisor itself also closes admission before initiating shutdown.

| Operation | Result |
| --- | --- |
| `ensure(owner, template, key, settings, revision=...)` | A ready `ActivationReference`, or a retained outcome through `ActivationTerminated`. |
| `inspect(LogicalIdentity(scope, template, key))` | The current snapshot, or `None` when unknown or expired. |
| `list_activations(scope, limit=..., cursor=...)` | An `ActivationPage` with bounded items and an optional next cursor. |
| `stop(reference)` | A `CleanupOutcome` for that current generation. |
| `reactivate(owner, terminal_reference)` | One successor generation, shared by matching transition retries. |
| `close_owner(owner)` / `close()` | A `CleanupReport` containing activation snapshots and a `complete` property. |

Import `OwnerHandle`, `ActivationPage`, `ActivationTerminated` and `CleanupReport`
from `cliffracer.runners.supervisor`. Contract values and the other typed errors
live in `cliffracer.runners.contracts`.

Matching concurrent requests share one accepted startup and one capacity slot.
Different owners, revisions or normalized settings for an existing logical
identity raise `ActivationConflict`. Cancelling a caller leaves accepted
startup or cleanup owned by the supervisor. Readiness waits use `wait_timeout`;
cleanup operations use their finite cleanup budgets.
Startup succeeds only after lifecycle startup and a broker request to the
activation's describe route verify the complete template contract.

`ActivationTerminated.snapshot` contains the outcome and cleanup status. A
matching ensure on a stopped or failed batch returns this outcome; it does not
repeat finished work. `reactivate` explicitly creates a new generation after
verified cleanup. Its address is different. An old reference cannot stop the
replacement, and its business RPC address cannot reach it. The supervisor never
interprets a business timeout as permission to replay the operation.

## Bounds, observation and retention

`SupervisorLimits` supplies positive finite limits. Starting, ready, stopping
and unfinished activations count toward `max_active`. Every retained generation
counts toward `max_records`; every retained owner counts toward `max_owners`.
Admission refuses excess work with `ActivationCapacityError`, including when
unexpired terminal guarantees occupy all record capacity. It does not evict an
unexpired outcome to accept a new batch.

Snapshots carry lifecycle state and broker connectivity separately. Temporary
reconnection preserves the generation. Permanent connection closure or child
termination initiates failed cleanup, including when `exit_on_closed=False`.
Ordinary business-handler errors leave the activation running. Inspection
excludes application settings, broker credentials and raw exception messages.
A failed activation is logged at warning level with its address and the type of the exception, a
failed lifecycle cleanup and an unfinished one at error level, and none of these lines carries
exception text. A permanent connection closure under a running lifecycle is reported as
`child broker connection closed`, and a lifecycle that ended as `child lifecycle terminated`.

Clean terminal snapshots advertise `retry_until` in UTC. The supervisor enforces
retention with its monotonic clock. Terminal generations, reactivation transition
records and closed owners expire after the configured retention period; active
or unfinished work remains retained. After expiry, control references raise
`ActivationUnavailable`. A new ensure can accept the same logical key after its
terminal record expires, with a fresh address. Use application-owned durable
operation identities for longer deduplication guarantees or host restarts.

Listing includes retained generations in admission order. The first page pins an
admission ceiling: later activations appear in a fresh listing. Subsequent pages
read current states and omit records expired since the previous page. They do
not repeat admissions. Cursors are scoped to the supervisor incarnation and
requested scope; `limit` must be between 1 and `max_page`.

## Incomplete cleanup

Cleanup is complete only when lifecycle teardown succeeds, tracked startup,
shutdown and business work finish, and subscriptions, the broker connection and
the health listener close. A returned stop coroutine alone is insufficient.
An exhausted deadline produces `unfinished`; that generation retains capacity
and cannot be replaced. If remaining work later finishes successfully, the
monitor publishes its clean terminal outcome. Failed cleanup remains unfinished
because the supervisor cannot establish that application resources were released.

Close reports are observations at the time close returns. Inspect the activation
for later completion. `unfinished_tasks` retains pending work for hosts managing
their own loop. Deadline-exhausted tasks are also registered with the existing
bounded synchronous loop host, which reports them instead of joining them forever.

All children share the process, event loop and failure boundary. Blocking Python
code can prevent any deadline from advancing. Factories and lifecycle hooks are
trusted; arbitrary unmanaged application tasks are outside this accounting.
Distributed ownership, subprocess termination, durable scheduling and state
recovery remain separate capabilities described in the
[virtual-service design](virtual-services.md).
