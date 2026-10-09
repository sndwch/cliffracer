# cliffracer-kv

NATS JetStream Key-Value and Object Store integration extension for Cliffracer services.

## Overview

`cliffracer-kv` provides NATS Key-Value and Object Store integration for Cliffracer services:

- **Service Extension (`KvExtension`)**: Declaratively bound to `self.kv` on your service.
- **Bucket Auto-Provisioning**: Automatic creation of KV buckets and Object Stores with configurable options.
- **Bucket-Level TTL**: Direct support for bucket-level TTL configurations (`ttl` parameter passed to JetStream bucket creation).
- **Serialization**: Automatic serialization and deserialization for Pydantic models, JSON dictionaries, lists, strings, and raw bytes. Dataclasses, datetimes, UUIDs, decimals, enums, sets and tuples are stored as their JSON form. A value with none, such as a plain object, raises `TypeError` naming its type, and so does a number JSON has no spelling for (`NaN`, infinity) and an iterator, generator or file object, which storing would consume (read it first). So does a `SecretStr`, a `SecretBytes` or a generic `Secret[...]`, alone or held at any depth by a model, a dataclass, a dict (as a key or a value) or a list, or as a model's extra or a computed field's value: its dump is a mask, and a bucket is readable by every client with access to it, so a caller who means to store the secret passes `get_secret_value()`. What pydantic writes as a string, such as a path or a `timedelta`, is stored as that string.
- **Optimistic Concurrency**: Supports revision-based updates and optimistic concurrency control.
- **Atomic Creation**: Insert an absent or deleted key without replacing a live value.
- **Watches and History**: Watch changes with revision metadata and read a selected revision.
- **Object Store Support**: Support for storing, retrieving, deleting, and listing large objects via NATS ObjectStore.

## Installation

```bash
uv add cliffracer-kv
```

## Usage

```python
from pydantic import BaseModel
from cliffracer import CliffracerService
from cliffracer_kv import KvExtension, BucketConfig

class UserProfile(BaseModel):
    name: str
    email: str

class UserService(CliffracerService):
    kv = KvExtension(
        buckets=[
            BucketConfig(name="profiles", ttl=3600),
            "sessions",
        ],
        bucket_ttls={"sessions": 86400},
        object_stores=["media-assets"],
    )

    async def save_profile(self, user_id: str, profile: UserProfile):
        revision = await self.kv.put("profiles", user_id, profile)
        return {"saved": True, "revision": revision}

    async def get_profile(self, user_id: str) -> UserProfile | None:
        return await self.kv.get("profiles", user_id, as_type=UserProfile)
```

## Reading values back

A value is stored as the bytes it serializes to: a `str` as its UTF-8 text, a `bytes` as itself,
and everything else as JSON. `get()` with no `as_type` reads the text back as JSON when it is
valid JSON and as a string when it is not, so what comes back is not always what went in:

| stored | `get(...)` | `get(..., as_type=str)` |
|---|---|---|
| `"hello"` | `"hello"` | `"hello"` |
| `"123"` | `123` | `"123"` |
| `"true"` | `True` | `"true"` |
| `"null"` | `None` | `"null"` |
| `'{"a": 1}'` | `{"a": 1}` | `'{"a": 1}'` |
| `b"hello"` | `"hello"` | `"hello"` |
| `""` or `b""` | `""` | `""` |

`as_type=str` and `as_type=bytes` return exactly what was stored; a Pydantic model type
validates it; `as_type=dict` and `as_type=list` raise `ValueError` when the stored JSON is any other
type, a stored `null` included (`the stored value is of type NoneType, not dict`). A stored `"null"` reads back as `None`, which is also what a missing key returns
when `default` is `None`: pass a `default` that is not a possible value to tell them apart. The
wire format is plain text so that other clients read the same bucket.

A Pydantic model is stored as its JSON under its field names, under its aliases, or with each field
where its validation alias reads it (its first `AliasChoices` member, the structure its `AliasPath`
names, each nested model in the form its own class reads), the first its own class reads back as the
same model, so `get(..., as_type=Model)` returns the model that was written. The last form is
written only when neither dump is read back, and only as the models write JSON: one holding a NaN or
an infinity is not offered unless every model in it has `ser_json_inf_nan="constants"`, which writes
them as `NaN` and `Infinity`; each model writes the values it owns under its own config.

"The same model" is judged by declared type, over what a dump writes: a value at a typed
position reads back as the value it held (NaN as NaN); a value at a position declared `Any` or
`object`, at any depth (a field, a `list[Any]` or `tuple[Any, ...]` item, a `dict[str, Any]` value,
an `Optional` or union arm, a bare `dict` or `list`; also written through a type alias such as
`type Payload = dict[str, Any]`, or as a `TypeVar` with no bound, which a model validates as `Any`),
and every extra, reads back with the same JSON form, which is all such a position promises, so a
`datetime` held there is stored and reads back as its ISO string, as before. A `TypeVar` with a
bound is judged as its bound, and one with constraints as the union of them. A value in a union is
judged under every arm it is an instance of, so it is compared by its JSON form only where each such
arm promises no more: `dict[str, Colour] | dict[str, Any]` holding an enum is compared by value,
and is refused, since the enum reads back as its string. A private attribute and a field declared `exclude=True` are not stored and
are not compared. A base class that declares the model's fields reads the same bytes through
`get(as_type=Base)`, so the form stored is one every such class reads back as the model where one
exists, else the form earlier releases stored if the model's own class reads it back, else one no
base reads worse than that; else the write is refused. A model that
reads back as other values from every form (a `field_serializer` that changes a typed field's value,
a base class that reads a field by name while the model reads it only through its validation alias)
is not written: `put()`, `create()` and `put_object()` raise
`ModelDoesNotReadBackError`, naming the model and what each form read back as. To store such a
model anyway, store `model_dump_json()` or a dict.

A value at a typed position is compared by value, so a value equal to what it reads back as is
stored even when it reads back as another class its declared union allows. A `str`-mixin enum or
`StrEnum` member at `Colour | str`, or an `IntEnum` member at `Level | int`, is written as its value,
and `get(as_type=...)` returns the plain `str` or `int`, which equals the member. To read the member
back, declare the field as the enum alone, or as
`Annotated[Colour | str, Field(union_mode="left_to_right")]`, which tries the enum first and still
reads any other text as `str`. A plain `Enum` member at `Colour | str` reads back as another value
(`Colour.RED != "red"`), so it is refused. RPC calls and events treat these values the same way.

## Connections

By default the extension uses the service's own connection: the service's JetStream context
when it has one (it runs with `jetstream_enabled`), and otherwise a context built from its
connection, so KV needs no `jetstream_enabled`. `KvExtension(nc=...)`
or `KvExtension(js=...)` takes a connection or a JetStream context instead, given
where the extension is declared, and wins over the service's: a context if one is given, else a
context built from the connection. It is one object shared by every service built
from that declaration, never a copy: wrapping it in `SharedDependency` is
accepted and means the same. Everything else a declaration carries is copied for
each service.

## Bucket configuration

`BucketConfig` exposes the native KV options: description, history, bucket TTL,
size limits, replicas, storage, placement, republishing, direct reads and expiry
marker retention. Storage accepts `StorageType.FILE`, `StorageType.MEMORY`, or
the lower-case strings `"file"` and `"memory"`.

A configuration that cannot work is refused with `BucketConfigError` when it is
built, naming the bucket and the field: a name with anything but letters, digits,
`_` and `-` (a `.` belongs in a key, not a name); a `ttl` that is not a number of seconds or
a `timedelta` (a bool or a string is refused), or is negative, not finite, or
under 100 ms (`0` means no expiry); `history` outside 1 to 64; `replicas` under 1; a
`storage` other than the two above; `max_bytes` or `max_value_size` that is not `-1` or a
whole number of at least 1; a `direct` that is not a bool; a `description` that is not a
string; a `placement` or `republish` that is not the native type; a `limit_marker_ttl` that
is not a whole number of seconds of at least 1. A dictionary with an option the config does
not have is refused, naming it, and so is a nested `placement` or `republish` dictionary
the native type cannot be built from. A declaration is a name, a configuration or a
dictionary of options: a single one passed as `buckets=` is one bucket, a collection of them
(a list, tuple, set or generator) is one bucket each, and an element that is not a declaration
is refused. A `buckets=` that is neither, such as a number or a `bytes` that would be read as its
byte values, is refused when the extension is built, naming the option. A bucket declared twice with different options is refused, naming it. An unset `ttl`, whether absent or `None`, takes the
`bucket_ttls` value. Placement and republishing use the native NATS types:

```python
from nats.js.api import Placement, RePublish, StorageType
from cliffracer_kv import BucketConfig, KvExtension

kv = KvExtension(buckets=[BucketConfig(
    name="inventory",
    storage=StorageType.FILE,
    placement=Placement(cluster="central", tags=["retail"]),
    republish=RePublish(src=">", dest="inventory.audit.>", headers_only=False),
    direct=False,
)])
```

Dictionary declarations accept nested dictionaries for `placement` and
`republish`, with the same field names as their native types. Republish subject
patterns are broker subjects; the extension passes them verbatim. Choose a
destination outside the bucket's subjects to avoid a republish loop.

Placement is included in the initial stream creation request. The broker
decides whether the requested cluster and tags can host the bucket. `direct`
sets the stream's direct-read permission; opening a bucket uses the broker's
effective setting. Both `False` and `True` are preserved. Omit it to use the
native default. Existing buckets are opened without changing their settings;
use `status()` to inspect them. When an option the declaration sets differs from the
existing bucket's, a WARNING at start names it, with both values, `republish` and
`placement` included; an option left at its default is not compared. A bucket the service
opened earlier, through `get_bucket()` in `on_startup`, is reported once and keeps its handle
when the extension starts. Marker retention additionally follows the
validation contract below.

An absent key or object returns the documented default, `None`, or empty list.
If a cached handle's backing stream has disappeared, the native JetStream
`NotFoundError` reaches the caller instead. Storage loss is never presented as
ordinary missing business data.

## Reservations and revisions

```python
import nats.js.errors

async def reserve_stock(service):
    try:
        revision = await service.kv.create("reservations", "stock.sku1", {"order": "order-a"})
    except nats.js.errors.KeyWrongLastSequenceError:
        pass  # Another order reserved this item.
    else:
        revision = await service.kv.put(
            "reservations", "stock.sku1", {"order": "order-a", "status": "confirmed"}, revision=revision
        )
        await service.kv.delete("reservations", "stock.sku1", last=revision)
```

`create()` returns the broker revision and serializes values like `put()`.
`put(..., revision=n)` and `delete(..., last=n)` refuse a stale revision with
the native NATS concurrency error: `KeyWrongLastSequenceError` for create/update,
or `BadRequestError` with `err_code=10071` for delete. `delete(..., last=n)` takes a positive
integer revision and raises `ValueError` for any other value, as `get(..., revision=n)` does:
a `0` or a negative number would otherwise delete with no check at all.
A deleted or purged key may be created again.

`delete()` and `purge()` return `None`. Each writes a marker whether or not the key exists
(the broker is not asked first), so a delete of a missing key succeeds and leaves a marker.

`get(..., revision=n)` reads a particular revision, using the same `as_type`
and `default` arguments as a current read. A revision outside the bucket's
retained history, or belonging to another key, returns `default`. Configure
`BucketConfig(history=...)` to retain more than the latest value. Use
`get_bucket()` and the native entry's `revision` when a current value and its
revision must be read together for CAS.

## Errors

`KvError` is the base of the errors this package raises itself: `BucketConfigError`
for a configuration that cannot work, `JetStreamUnavailableError` when there is no
JetStream context, and `ModelDoesNotReadBackError` for a model a write cannot store as itself,
which is a `TypeError` too, as every value a write cannot store is. Errors from nats-py reach the caller unchanged: `KeyWrongLastSequenceError`
and `BadRequestError` for a stale revision, `BucketNotFoundError` for a missing bucket when
`create_if_missing` is off, and the server errors a refused bucket configuration returns.
`except KvError` therefore does not catch them.

## Watching changes

```python
async def watch_profiles(service):
    async with service.kv.watch("profiles", "user.*") as watcher:
        async for entry in watcher:
            if entry is None:
                continue  # The initial snapshot is complete; live updates follow.
            print(entry.key, entry.revision, entry.operation, entry.value)
```

The context releases its subscription on normal exit, an exception, or
cancellation. Keep it inside the service's running lifetime. Reopening a watch
reads the current snapshot; it does not resume a durable consumer cursor.
`include_history=True` includes retained revisions in that snapshot.

`watchall(bucket)` watches every key. Both methods accept `include_history`,
`ignore_deletes`, `meta_only`, and `inactive_threshold`. The watcher is the
native nats-py `KeyWatcher`: entries carry bytes and revision/operation metadata,
as in `history()`. `watcher.updates(timeout=...)` reads one event with a timeout.
Deletion and purge entries have operations `DEL` and `PURGE`; `None` is a
snapshot boundary, not a value or the end of the watch.

## Broker status and runtime bucket defaults

`await service.kv.status(bucket)` reads a fresh native `BucketStatus` from the
broker. It includes `values` (stored revisions, including tombstones), `history`,
`ttl`, and `stream_info` with the broker's effective configuration.

```python
async def open_sessions(service):
    sessions = await service.kv.get_bucket(
        "sessions", default_config=BucketConfig(name="sessions", ttl=300, history=5)
    )
```

The default is used only when no bucket declaration exists in this extension.
A constructor declaration takes precedence. The default must name the requested
bucket and is retained for later access. An existing broker bucket is opened
without reconfiguration. Distributed cron uses this public argument for its
default retention; it does not edit the extension's configuration tables.

## Health

The extension adds its attribute's name (`kv` by default) to `/health` with `connected`, the
open buckets and the open object stores. `connected` reads the NATS client's connection state
on each call, so it is false while the client is disconnected or reconnecting.

Starting the extension also registers a health dependency under the same name. Its probe
raises unless the client is connected and every open bucket and object store answers a
`status()` call, and it is bounded at two seconds across all of them. A deleted bucket, a lost
stream or a closed connection therefore make the service report `unhealthy`, and readiness
follows. A bucket opened later, by `get_bucket()`, is probed from then on.

## Per-key expiry

```python
async def expire_offer(service):
    await service.kv.create("offers", "offer.sku1", {"discount": 10}, ttl=30)
    await service.kv.purge("offers", "offer.sku1", ttl=5)
```

`ttl` accepts whole positive seconds or a `timedelta` containing whole positive
seconds. Fractional values are refused rather than truncated by nats-py.
On `create()`, it expires the value. On `purge()`, it expires the purge marker
after purging prior revisions. `put()` does not accept per-key TTL.

These operations require NATS Server 2.11+ and `allow_msg_ttl=True` on the
backing stream. The extension reads the actual bucket configuration before a
TTL write and raises `BucketConfigError` if the broker has not enabled it.
Without a TTL argument, ordinary KV operations also work on older brokers.

### Expiry notifications

Declare `BucketConfig(name="offers", limit_marker_ttl=30)` to retain
server-generated expiry markers for 30 seconds. Both bucket-age and per-key
expiry produce a native watch entry with operation `PURGE` when the last
retained value for that key expires. Marker retention describes the lifetime
of that tombstone.

Expiry markers require released NATS Server 2.11.2+ on **every JetStream node**,
including during rolling upgrades. The extension refuses an older connected
server, the native client verifies the broker API capability, and the extension
checks the resulting bucket's marker retention. A declaration that disagrees
with an existing bucket raises `BucketConfigError`; it does not reconfigure it.
Ordinary buckets without markers retain the per-key TTL requirements above.

With `history=1`, a per-key TTL may be shorter than marker retention. With
`history>1`, `create(..., ttl=...)` and `purge(..., ttl=...)` refuse durations
shorter than marker retention because the broker would extend them. Choose
retention and history explicitly when short expiry times matter.

Reads through the extension and handles returned by `get_bucket()` treat
recognized server markers as deleted keys. An expired key can be created again
while its marker is retained: the native create path compares the marker's
revision, so competing creators cannot overwrite the winner. An empty live
value remains a live key. Raw JetStream contexts obtained through `js` keep
their native behavior.

Watches describe retained broker state and live changes; they are not durable
expiry logs. If older revisions remain, the key is not empty and there is no
expiry marker for that removal. Applications must not use the absence of a
watch event as proof that a value still exists.

A lease heartbeat requires revision-checked renewal and an explicit deadline
policy. An expiring record alone cannot fence a disconnected or paused holder.
The proposed actor lease contract is specified in
[ADR-0019](../../docs/decisions.md#adr-0019-virtual-actor-model-and-state-fencing).

## Object values

`put_object()` stores every value with the same encoding as `put()`, and refuses the same
ones with the same `TypeError`. Strings are UTF-8 and bytes remain bytes. A file object is
the one thing passed through to nats-py for streaming, and it is not consumed by `put()`. `get_object()` returns the native object result;
decode its `data` explicitly or use `writeinto` for a file destination.
