# NATS broker permissions

`cliffracer.broker_permissions.broker_permissions` derives subject grants from
a service class or a serialized `Description` and its `ServiceConfig`. It is a
pure configuration helper: it constructs no service, connects to no broker and
changes no credentials. Install its output in the broker's user configuration.

## Service and client roles

Choose a dedicated inbox prefix for each broker role. Service replicas sharing
a durable push consumer use the same prefix because its delivery inbox remains
part of the durable consumer's configuration across restarts.

The prefix's grant is `<prefix>.>`, a subscribe permission over everything beneath it, so the
prefix must lie outside the role's own subjects. `broker_permissions` refuses one that sits in
the service's environment prefix (or its namespace, when it has no environment prefix) or that
covers a subject the role is granted, and the refusal names what the grant would cover.
`_INBOX.orders` and a dedicated prefix such as `orders.replies` are accepted.

```python
from cliffracer import ServiceConfig
from cliffracer.broker_permissions import broker_permissions


def order_roles(orders_class):
    config = ServiceConfig(
        name="orders",
        namespace="retail",
        subject_prefix="east",
        nats_inbox_prefix="_INBOX.order_workers",
    )
    workers = broker_permissions(orders_class, config, role="service")
    customers = broker_permissions(
        orders_class,
        config,
        role="client",
        inbox_prefix="_INBOX.customers",
    )
    return config, workers.to_nats_permissions(), customers.to_nats_permissions()
```

Each returned dictionary is a NATS user's `permissions` block. Configure that
user's authentication separately. The helper returns no passwords or tokens.

`BrokerPermissions.publish` and `.subscribe` are sorted, immutable tuples of
wire subjects. The renderer uses explicit deny-all entries for empty directions;
omitting a permission direction or supplying an empty allow list can leave that
direction unrestricted in broker configuration.

The service role subscribes to its actual RPC and async-RPC wildcard endpoints,
its describe endpoint, core event listeners and its own inbox family. RPC methods
are dispatched inside those wildcard subscriptions, so replacing them with
per-method grants would prevent the runtime from subscribing. Namespace and
environment prefixes follow the same builders as the running service;
cross-namespace listeners retain the environment boundary.

The client role publishes to the describe endpoint and each named RPC method.
`allow_async=True` additionally grants each method's async endpoint. Its inbox
prefix must be supplied explicitly; it never inherits the target service's inbox.
The service role reads its inbox prefix from `config.nats_inbox_prefix`; an
explicit override must match that configuration. The unscoped `_INBOX` prefix,
wildcards, empty tokens and control characters are refused.

## Configure the connecting client

An owned generated-client connection accepts `inbox_prefix`:

```python
async def reserve_order(orders_client, broker_url):
    async with orders_client(
        nats_url=broker_url,
        subject_prefix="east",
        inbox_prefix="_INBOX.customers",
    ) as client:
        return await client.reserve(quantity=3)
```

For an existing authenticated connection, set `inbox_prefix` on `nats.connect`
when creating it, then pass that connection as `nc` to the generated client.
A client cannot change the prefix on a connection it borrows. Service connections
apply `ServiceConfig.nats_inbox_prefix` when they connect. Leaving the setting
unset retains the NATS client's default connection behavior, but the permission
helper requires an explicit scoped prefix.

A client whose role is confined to an inbox prefix and that is opened without `inbox_prefix` cannot
subscribe to its reply inbox. The broker reports that to the connection and not to the request, which
times out. The `RpcTimeoutError` then says what the broker reported while that request waited, naming
the subject it gave, and points at `inbox_prefix=`: `orders.rpc.ping did not answer within 3s; the
broker reported while it waited: permissions violation for subscription to "_inbox.<id>.*". A client
role the broker confines to an inbox prefix needs inbox_prefix= naming it.` Every violation reported
during the wait is named, oldest first, joined by `; then`. The broker reports a
refusal once, so a later request on the same connection times out without the reason. A timeout with
no violation reported during its wait reads as it always did.

## Replies and outbound application traffic

A service receives NATS `allow_responses` with one reply and a finite expiration of
`response_ttl`, 30 seconds by default. An expired grant cannot deliver a late response, even if
the handler finishes successfully, and a second reply on the same request is refused.

With `max_rpc_processing_time` set, a service's role is refused unless `response_ttl` is at least
that bound plus `RESPONSE_GRANT_MARGIN` (1.0 second); the refusal names the setting and the value
to raise it to. The grant's clock starts when the broker routes the request, before the service's
deadline does, so the reply a service sends at its deadline, `deadline_exceeded`, is published
just after a grant of exactly the bound has expired: measured on nats-server 2.10.29 and 2.11.2,
the broker refused that reply as a permissions violation and the caller timed out instead. Choose
`response_ttl` as the longest a caller should get an answer for, and at least the bound plus the
margin. Without `max_rpc_processing_time`, `response_ttl` alone bounds a reply.

A service with a method that streams its reply (see the API reference) receives
`allow_responses` with `max: -1`, any number of replies, for `response_ttl`, since a stream is one
message per item and then its envelope. The broker drops a reply after the grant expires without a
word to either side, so such a role is also refused unless `max_rpc_processing_time` is set, which
bounds every stream; the same margin covers the envelope ending a stream cut at its deadline, and
the refusals name the streaming methods. `response_ttl=None` leaves the grant out, as for any
service.

Response grants follow the reply subject supplied by the request; they do not
authenticate that subject as a particular customer's inbox. A deployment that
must predeclare reply destinations can set `response_ttl=None` and supply explicit
reply subjects in `extra_publish` instead.

Every service receives its concrete dead-letter publishing subject. With
JetStream enabled it also receives the declared stream subjects. Other outbound
events, RPC calls to peer services, dynamically registered listeners and extension
traffic are not inferred from Python handler bodies. Supply their reviewed wire
subjects through `extra_publish` and `extra_subscribe`. These are already scoped:
the helper applies no additional namespace or environment prefix to them.

For [typed template outputs](typed-outputs.md),
`template.bind_outputs(accepted_settings, config).publish_subjects` provides
the accepted families for `extra_publish`. Review and install these grants
explicitly; an output binding alone changes no broker permissions.

`assert_rpc_permissions` in `cliffracer.testing` checks a maintained grant against
the current method table and names missing RPCs and the describe endpoint. Pass
`role="service"` to check subscriptions or leave the default `role="client"` to
check publishing; `allow_async=True` includes async calls. This is a named-call
coverage assertion, not certification of arbitrary broker policies or extensions.

## JetStream grants and deployment boundaries

The core runtime uses the default JetStream API prefix. Permissions enumerate
stream creation, optional updates, and the individual durable consumers' creation,
inspection, pull-fetch and acknowledgment subjects. No blanket `$JS.API.>` grant
is generated. Stream and durable names receive the configured environment prefix
once. Consumer acknowledgment grants cover the broker's ordinary and domain-aware
acknowledgment address forms.

Each durable listener must resolve to exactly one declared stream. Missing or
ambiguous declarations are refused. JetStream delivery arrives through inboxes;
granting the listener's filter subject alone cannot admit it. The role's only inbox
grant is its own prefix, and bind mode refuses a pre-existing push consumer whose
delivery subject lies outside that prefix. The helper does not broaden access to
adopt an inbox owned by another role.

The default `jetstream_resource_mode="provision"` reads account-wide stream
metadata to check overlap, so its policy includes `STREAM.LIST`. Durable setup
discovers the stream by subject and also needs `STREAM.NAMES`. These are metadata
reads across the NATS account, even though stream and consumer writes name
individual resources.

Set `jetstream_resource_mode="bind"` when an operator creates the streams and
durable consumers before the service starts. Bind mode reads every resource by
its declared name and validates every `StreamSpec` field; consumer filter, push
or pull shape, delivery group and tuning; and a push consumer's
delivery subject under `nats_inbox_prefix`. A missing or incompatible resource
fails startup with the name and expected contract. The derived service role has
named stream and consumer INFO access, pull-fetch and acknowledgment access, and
the declared application subjects. It has no STREAM.LIST, STREAM.NAMES, stream
create/update, or consumer create grants. `jetstream_update_streams=True` is
invalid in bind mode because resource changes remain the operator's job.

Provisioning credentials assume trusted application code. NATS subject permissions
do not validate every JSON field in a stream or consumer creation request; granting
a named create operation does not enforce its declared subjects or filter payload.
Use separate NATS accounts when applications require a stronger isolation boundary.
Arbitrary extension resources, alternate JetStream API prefixes and flow-control
consumer customization require separately reviewed policies. The core path is
exercised against NATS 2.10.29 and 2.11.2.

See [NATS authorization](https://docs.nats.io/learn/security/authorization) and
[response grants](https://docs.nats.io/reference/config/authorization/users/permissions/allow_responses/)
for the broker's enforcement rules.
