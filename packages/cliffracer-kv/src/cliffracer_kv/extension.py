"""NATS JetStream Key-Value and Object Store extension."""

from __future__ import annotations

import asyncio
import inspect
import io
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from typing import Any, cast

import nats.js.errors
from loguru import logger
from nats.js.kv import KV_DEL, KV_PURGE, KeyValue

from cliffracer.core.extension import Extension, ExtensionSetupContext, SharedDependency

from .config import (
    BucketConfig,
    ObjectStoreConfig,
    declared_name,
    normalize_message_ttl,
    normalize_ttl_seconds,
)
from .drift import bucket_drift, declares_options, describe, object_store_drift
from .errors import BucketConfigError, JetStreamUnavailableError
from .markers import expiry_aware_bucket, require_expiry_markers
from .provisioning import create_bucket
from .serialization import deserialize_value, serialize_value

#: How long one health probe of the extension may take, across every open bucket and store.
HEALTH_PROBE_TIMEOUT = 2.0


async def _snapshot(
    kv: KeyValue, keys: str, *, include_history: bool, meta_only: bool
) -> list[KeyValue.Entry]:
    """Every entry a watch on `keys` delivers up to the end of its snapshot.

    nats-py ends a snapshot at its None marker, which it can queue before the
    entries: it reads the consumer's pending count and its own received count,
    and under load both are 0 while the entries are still unread in the socket.
    So the snapshot ends here at the first entry whose `delta` is 0, or at a
    marker after an entry, which nats-py queues only once a message has left
    nothing pending. A marker before any entry is believed only if the stream
    holds no message for `keys`. Each wait is bounded by the JetStream context's timeout, so
    entries that never arrive raise rather than read as none.

    Deletions and purges are entries; callers that list keys drop them. This
    reads nats-py's private `KeyValue._js`, `_stream` and `_pre`, which its own
    `get()` uses to address the same stream and subjects.
    """
    watcher = await kv.watch(keys, include_history=include_history, meta_only=meta_only)
    timeout = kv._js._timeout
    entries: list[KeyValue.Entry] = []
    try:
        while True:
            entry = await watcher.updates(timeout=timeout)
            if entry is None:
                # Queued after an entry, the marker follows a message that left
                # nothing pending. Queued before any, it may precede them.
                if entries or not await _stream_holds(kv, keys):
                    return entries
                continue
            entries.append(entry)
            if entry.delta == 0:
                return entries
    finally:
        await watcher.stop()


async def _stream_holds(kv: KeyValue, keys: str) -> bool:
    """Whether the bucket's stream holds any message for `keys` (a key or ">")."""
    if keys == ">":
        info = await kv._js.stream_info(kv._stream)
        return bool(info.state.messages)
    try:
        await kv._js.get_msg(kv._stream, subject=f"{kv._pre}{keys}", direct=kv._direct)
    except nats.js.errors.NotFoundError:
        await _require_backing_stream(kv)
        return False
    return True


async def _require_backing_stream(handle: Any) -> None:
    """Raise when a cached KV or Object Store handle has lost its stream.

    nats-py translates the same JetStream ``NotFoundError`` into a key or
    object miss even when the stream itself disappeared. Checking the handle's
    stream after an apparent item miss keeps ordinary absence distinct from
    lost infrastructure without adding a round trip to successful reads.
    """
    await handle._js.stream_info(handle._stream)


def _declarations(value: Any, option: str) -> tuple[Any, ...]:
    """The declarations the constructor argument `option` holds.

    A single name, configuration object or dictionary is one declaration. Iterating it would
    read a name as its letters and a dictionary as its keys. What is not a declaration or a
    collection of them, a number or a `bytes` that would be read as its byte values, is refused.
    """
    if value is None:
        return ()
    if isinstance(value, str | dict | BucketConfig | ObjectStoreConfig):
        return (value,)
    if isinstance(value, bytes | bytearray | memoryview) or not isinstance(value, Iterable):
        raise BucketConfigError(
            f"{option} takes a name, a configuration, a dictionary or a collection of them; "
            f"it was given {value!r} ({type(value).__name__})"
        )
    return tuple(value)


class KvExtension(Extension):
    """JetStream Key-Value and Object Store client extension.

    Bound as a class attribute to provide KV bucket and Object Store handle
    caches, serialization, and automated bucket provisioning during service setup.
    """

    name: str = "kv"

    #: The constructor parameters that take a live connection. A connection is
    #: one object shared by every service built from a declaration, so it is
    #: carried through binding as it is rather than copied.
    _SHARED_CONNECTIONS = ("nc", "js")

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        """Declare `nc=` and `js=` as shared, so binding hands each service the same object.

        Binding rebuilds the extension from the arguments it was declared with and
        copies each one; a connection cannot be copied, and an unconnected client
        that is copied is a different client from the one connected later. The
        arguments are recorded wrapped in `SharedDependency`, which binding
        unwraps, while `__init__` still receives them as written.
        """
        try:
            bound = inspect.signature(cls.__init__).bind_partial(None, *args, **kwargs)
        except TypeError:
            # Arguments `__init__` will refuse; let it say so.
            return super().__new__(cls, *args, **kwargs)
        for key in cls._SHARED_CONNECTIONS:
            value = bound.arguments.get(key)
            if value is not None and not isinstance(value, SharedDependency):
                bound.arguments[key] = SharedDependency(value)
        return super().__new__(cls, *bound.args[1:], **bound.kwargs)

    def __init__(
        self,
        buckets: str
        | BucketConfig
        | dict[str, Any]
        | Sequence[str | BucketConfig | dict[str, Any]]
        | None = None,
        bucket_ttls: Mapping[str, float | int | timedelta] | None = None,
        object_stores: str
        | ObjectStoreConfig
        | dict[str, Any]
        | Sequence[str | ObjectStoreConfig | dict[str, Any]]
        | None = None,
        object_store_ttls: Mapping[str, float | int | timedelta] | None = None,
        create_if_missing: bool = True,
        nc: Any = None,
        js: Any = None,
    ) -> None:
        # DECLARED here, CREATED in setup(). bind() reconstructs the extension
        # through create_instance(), so each bound instance runs this
        # constructor and gets its own dictionaries.
        self._init_buckets = _declarations(buckets, "buckets")
        self._init_bucket_ttls = dict(bucket_ttls) if bucket_ttls else {}
        self._init_object_stores = _declarations(object_stores, "object_stores")
        self._init_object_store_ttls = dict(object_store_ttls) if object_store_ttls else {}
        self.create_if_missing = create_if_missing
        self._explicit_nc = nc
        self._explicit_js = js

        self._kv_stores: dict[str, Any] | None = None
        self._obj_stores: dict[str, Any] | None = None
        self._bucket_configs: dict[str, BucketConfig] | None = None
        self._object_store_configs: dict[str, ObjectStoreConfig] | None = None
        self._js: Any = None
        # One lock per bucket or store name, so concurrent first opens of one name open it once.
        self._open_locks: dict[tuple[str, str], asyncio.Lock] = {}

    # -- lifecycle -------------------------------------------------------------

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        """Run before broker connect: build per-instance config tables and caches."""
        # Bucket and object-store names are global to the broker, so they carry
        # the service's environment prefix the way subjects and streams do. The
        # caches below stay keyed by the name the caller used; only the name that
        # reaches the broker is prefixed.
        # Through `ctx` defensively, not `ctx.service_config` directly: the
        # context is a stand-in in some callers -- the benchmark harness passes a
        # SimpleNamespace carrying only `nc` and `js`. A caller with no config has
        # no prefix to apply, so the default is no prefix, which is also what an
        # unprefixed environment gets.
        self._subject_prefix = getattr(getattr(ctx, "service_config", None), "subject_prefix", None)
        self._declare()

    async def start(self) -> None:
        """Run after broker connect: resolve JetStream and auto-provision stores."""
        self._ensure_initialized()
        self._js = self._resolve_js()

        # Auto-provision declared buckets
        # Through the cache the service's own `on_startup` may already have filled: a bucket it
        # opened is reused, not opened a second time, so it is not reported twice and its handle
        # is not replaced under code that holds it.
        assert self._bucket_configs is not None
        for cfg in self._bucket_configs.values():
            await self.get_bucket(cfg.name)

        # Auto-provision declared object stores
        assert self._object_store_configs is not None
        for os_cfg in self._object_store_configs.values():
            await self.get_object_store(os_cfg.name)

        self._register_health_probe()

    async def stop(self) -> None:
        """Run on shutdown: clear cached handles and reset state."""
        if self._kv_stores is not None:
            self._kv_stores.clear()
        if self._obj_stores is not None:
            self._obj_stores.clear()
        self._js = None

    def _register_health_probe(self) -> None:
        """Make the service's readiness follow the buckets, not only the extension's own flag.

        A probe that asks the broker about every open bucket and store runs on each health check,
        bounded by `HEALTH_PROBE_TIMEOUT`. A deleted bucket, a lost stream or a closed connection
        then fail it, and a service that needs the store reads as unhealthy.
        """
        add_dependency = getattr(self.service, "add_dependency", None)
        if add_dependency is not None:
            add_dependency(self.name, self._probe, timeout=HEALTH_PROBE_TIMEOUT)

    async def _probe(self) -> None:
        """Raise unless the connection is up and every open bucket and store answers."""
        if not self._connected():
            raise JetStreamUnavailableError("the NATS connection is not up")
        handles = [*(self._kv_stores or {}).values(), *(self._obj_stores or {}).values()]
        await asyncio.gather(*(handle.status() for handle in handles))

    def _connected(self) -> bool:
        """Whether the NATS client behind the JetStream context is connected right now.

        A JetStream context is a wrapper that does not track the connection, so its existence
        says nothing after a disconnect. When no client can be found to ask, the context's
        existence is all there is.
        """
        if self._js is None:
            return False
        client = next(
            (
                nc
                for nc in (
                    getattr(self._js, "_nc", None),
                    self._explicit_nc,
                    getattr(self.service, "nc", None),
                )
                if nc is not None
            ),
            None,
        )
        return True if client is None else bool(getattr(client, "is_connected", True))

    def health_details(self) -> dict[str, Any] | None:
        """Report whether the connection is up and the active buckets and object stores."""
        return {
            "connected": self._connected(),
            "buckets": list(self._kv_stores.keys()) if self._kv_stores else [],
            "object_stores": list(self._obj_stores.keys()) if self._obj_stores else [],
        }

    # -- internal helpers ------------------------------------------------------

    def _ensure_initialized(self) -> None:
        """Ensure per-instance structures exist even if setup() was bypassed."""
        if self._kv_stores is None:
            self._declare()

    def _declare(self) -> None:
        """Build the handle caches and the configuration tables from the constructor's declarations."""
        self._kv_stores = {}
        self._obj_stores = {}
        self._bucket_configs = self._declared(
            BucketConfig, self._init_buckets, self._init_bucket_ttls
        )
        self._object_store_configs = self._declared(
            ObjectStoreConfig, self._init_object_stores, self._init_object_store_ttls
        )

    @staticmethod
    def _declared(
        config_type: Any, declarations: Sequence[Any], ttls: Mapping[str, Any]
    ) -> dict[str, Any]:
        """One configuration per declaration, then one for each name only a ttl mentions.

        A declaration with no ttl of its own takes the one `ttls` holds for its name.
        """
        configs: dict[str, Any] = {}
        for value in declarations:
            name = declared_name(value, config_type)
            default_ttl = ttls.get(str(name)) if name is not None else None
            config = config_type.from_value(value, default_ttl=default_ttl)
            if config.name in configs and configs[config.name] != config:
                raise BucketConfigError(
                    f"{config_type.__name__[:-6]} {config.name!r} is declared twice with "
                    "different options; declare it once"
                )
            configs[config.name] = config
        for name, ttl in ttls.items():
            if name not in configs:
                configs[name] = config_type(name=name, ttl=ttl)
        return configs

    def _resolve_js(self) -> Any:
        """Resolve the active NATS JetStream context.

        What the declaration was given wins over what the service has: an explicit `js`, then an
        explicit `nc`, then the service's `js` (set when it runs with `jetstream_enabled`), then
        a context built from the service's connection. The last is what a service with the
        default `jetstream_enabled=False` uses, so the extension works without it.
        """
        if self._js is not None:
            return self._js
        if self._explicit_js is not None:
            return self._explicit_js

        # Try explicit_nc.jetstream()
        if self._explicit_nc is not None:
            try:
                return self._explicit_nc.jetstream()
            except Exception as err:
                raise JetStreamUnavailableError(
                    f"Failed to create JetStream context from explicit NATS connection: {err}"
                ) from err

        # Try service.js
        if self.service is not None and getattr(self.service, "js", None) is not None:
            return self.service.js

        # Try service.nc.jetstream()
        if self.service is not None and getattr(self.service, "nc", None) is not None:
            try:
                js = self.service.nc.jetstream()
                if js is not None:
                    return js
            except Exception as err:
                raise JetStreamUnavailableError(
                    f"Failed to create JetStream context from service connection: {err}"
                ) from err

        raise JetStreamUnavailableError(
            "KvExtension has no JetStream context: it was given no js= or nc=, and the service "
            "it is declared on has no connection (it is used once the service has connected)."
        )

    @property
    def js(self) -> Any:
        """Direct access to the underlying JetStream context."""
        return self._resolve_js()

    def _wire_name(self, name: str) -> str:
        """The name this bucket or store has on the broker."""
        prefix = getattr(self, "_subject_prefix", None)
        return f"{prefix}_{name}" if prefix else name

    async def _report_drift(self, kind: str, config: Any, handle: Any, compare: Any) -> None:
        """Warn when an existing bucket or store differs from what this service declares.

        Opening an existing one applies no configuration, so the declaration is otherwise
        accepted and ignored. Reading the stream back never stops a service starting.
        """
        if not declares_options(config):
            return
        log = getattr(self.service, "logger", None) or logger
        try:
            stream = (await handle.status()).stream_info.config
            drift = compare(config, stream)
        except Exception as exc:  # noqa: BLE001 - the read is advisory
            log.warning(
                f"could not read the configuration of {kind.lower()} {config.name!r} to compare "
                f"it with this service's declaration: {type(exc).__name__}: {exc}"
            )
            return
        if drift:
            log.warning(describe(kind, config.name, drift))

    async def _ensure_bucket(self, config: BucketConfig) -> Any:
        """Retrieve existing KeyValue handle or create it with configured options (including TTL)."""
        js = self._resolve_js()
        self._js = js
        marker_ttl = (
            normalize_message_ttl(config.limit_marker_ttl)
            if config.limit_marker_ttl is not None
            else None
        )
        if marker_ttl is not None:
            require_expiry_markers(js)

        existing = True
        try:
            kv = await js.key_value(self._wire_name(config.name))
        except nats.js.errors.BucketNotFoundError:
            if not self.create_if_missing:
                raise
            existing = False

            # Auto-provision bucket with bucket-level TTL configuration
            params: dict[str, Any] = {"bucket": self._wire_name(config.name)}
            ttl_sec = normalize_ttl_seconds(config.ttl)
            if ttl_sec is not None:
                params["ttl"] = ttl_sec
            if config.description is not None:
                params["description"] = config.description
            if config.history != 1:
                params["history"] = config.history
            if config.max_bytes is not None:
                params["max_bytes"] = config.max_bytes
            if config.max_value_size is not None:
                params["max_value_size"] = config.max_value_size
            if config.replicas != 1:
                params["replicas"] = config.replicas
            if config.storage is not None:
                params["storage"] = config.storage
            if marker_ttl is not None:
                params["limit_marker_ttl"] = marker_ttl
            if config.placement is not None:
                params["placement"] = config.placement
            if config.republish is not None:
                params["republish"] = config.republish
            if config.direct is not None:
                params["direct"] = config.direct

            kv = await create_bucket(js, **params)
        if existing:
            await self._report_drift("Bucket", config, kv, bucket_drift)
        if marker_ttl is not None and (await kv.status()).marker_ttl != marker_ttl:
            raise BucketConfigError(
                f"Bucket {config.name!r} does not have the requested marker retention {marker_ttl}"
            )
        kv = expiry_aware_bucket(kv)
        assert self._kv_stores is not None
        self._kv_stores[config.name] = kv
        return kv

    async def _ensure_object_store(self, config: ObjectStoreConfig) -> Any:
        """Retrieve existing ObjectStore handle or create it with configured options."""
        js = self._resolve_js()
        self._js = js

        try:
            store = await js.object_store(self._wire_name(config.name))
            await self._report_drift("Object store", config, store, object_store_drift)
            assert self._obj_stores is not None
            self._obj_stores[config.name] = store
            return store
        except nats.js.errors.BucketNotFoundError:
            if not self.create_if_missing:
                raise

            params: dict[str, Any] = {"bucket": self._wire_name(config.name)}
            ttl_sec = normalize_ttl_seconds(config.ttl)
            if ttl_sec is not None:
                params["ttl"] = ttl_sec
            if config.description is not None:
                params["description"] = config.description
            if config.max_bytes is not None:
                params["max_bytes"] = config.max_bytes
            if config.replicas != 1:
                params["replicas"] = config.replicas
            if config.storage is not None:
                params["storage"] = config.storage

            store = await js.create_object_store(**params)
            assert self._obj_stores is not None
            self._obj_stores[config.name] = store
            return store

    # -- bucket & object store access ------------------------------------------

    async def get_bucket(self, bucket: str, *, default_config: BucketConfig | None = None) -> Any:
        """Get a bucket, using default_config only when no declaration exists.

        Constructor declarations take precedence. The default config belongs
        to this bucket and is retained for subsequent access. Existing broker
        buckets are opened without changing their configuration.
        """
        if default_config is not None and default_config.name != bucket:
            raise BucketConfigError("Default bucket configuration must name the requested bucket")
        self._ensure_initialized()
        assert self._kv_stores is not None
        if bucket in self._kv_stores:
            return self._kv_stores[bucket]

        # Callers that find the bucket unopened at the same moment would each provision it, and
        # each keep a handle of its own. The second to arrive finds it opened.
        async with self._open_lock("bucket", bucket):
            if bucket in self._kv_stores:
                return self._kv_stores[bucket]

            assert self._bucket_configs is not None
            cfg = self._bucket_configs.get(bucket)
            if cfg is None:
                ttl = self._init_bucket_ttls.get(bucket)
                cfg = default_config or BucketConfig(name=bucket, ttl=ttl)
                self._bucket_configs[bucket] = cfg

            return await self._ensure_bucket(cfg)

    async def get_object_store(self, bucket: str) -> Any:
        """Get or auto-provision an ObjectStore handle."""
        self._ensure_initialized()
        assert self._obj_stores is not None
        if bucket in self._obj_stores:
            return self._obj_stores[bucket]

        async with self._open_lock("store", bucket):
            if bucket in self._obj_stores:
                return self._obj_stores[bucket]

            assert self._object_store_configs is not None
            cfg = self._object_store_configs.get(bucket)
            if cfg is None:
                ttl = self._init_object_store_ttls.get(bucket)
                cfg = ObjectStoreConfig(name=bucket, ttl=ttl)
                self._object_store_configs[bucket] = cfg

            return await self._ensure_object_store(cfg)

    def _open_lock(self, kind: str, name: str) -> asyncio.Lock:
        return self._open_locks.setdefault((kind, name), asyncio.Lock())

    # -- Key-Value API ---------------------------------------------------------

    async def _message_ttl(self, kv: KeyValue, ttl: float | int | timedelta | None) -> float | None:
        """Refuse TTL writes unless the broker reports support on this bucket."""
        if ttl is None:
            return None
        seconds = normalize_message_ttl(ttl)
        status = await kv.status()
        if status.stream_info.config.allow_msg_ttl is not True:
            raise BucketConfigError(
                "Per-key TTL requires NATS Server 2.11+ and a bucket with allow_msg_ttl enabled"
            )
        if status.marker_ttl:
            require_expiry_markers(kv._js)
            if status.history > 1 and seconds < status.marker_ttl:
                raise BucketConfigError(
                    "Message TTL must be at least the marker retention when history exceeds one"
                )
        return seconds

    async def create(
        self, bucket: str, key: str, value: Any, *, ttl: float | int | timedelta | None = None
    ) -> int:
        """Insert an absent or deleted key atomically and return its revision.

        An existing key raises nats.js.errors.KeyWrongLastSequenceError. TTL is
        in whole positive seconds and requires broker support on this bucket.
        """
        kv = await self.get_bucket(bucket)
        msg_ttl = await self._message_ttl(kv, ttl)
        return int(await kv.create(key, serialize_value(value), msg_ttl=msg_ttl))

    async def status(self, bucket: str) -> KeyValue.BucketStatus:
        """Read the bucket's current broker configuration and storage statistics."""
        kv = await self.get_bucket(bucket)
        return cast(KeyValue.BucketStatus, await kv.status())

    @asynccontextmanager
    async def watch(
        self,
        bucket: str,
        key_pattern: str = ">",
        *,
        include_history: bool = False,
        ignore_deletes: bool = False,
        meta_only: bool = False,
        inactive_threshold: float | None = None,
    ) -> AsyncIterator[KeyValue.KeyWatcher]:
        """Watch native KV entries, releasing the subscription on context exit.

        Entries carry raw bytes, revision and operation, as in history(). The
        None entry marks the end of the initial snapshot, not the watch. Use
        watcher.updates(timeout=...) or async iteration for subsequent changes.

        Under load the None can arrive before the snapshot rather than after
        it: nats-py queues it first when the server has sent the entries but
        the client has not yet read them. An entry whose `delta` is 0 is the
        last of the snapshot either way, except with `ignore_deletes`: a
        deletion or purge is then never delivered, so a history that ends on
        one has no such entry. Each entry's `delta` still counts the skipped
        messages after it, and the stream says whether only those are left.

        To know a replay is complete when deletions must be skipped, watch without
        `ignore_deletes` and skip `KV_DEL` and `KV_PURGE` entries: the entry whose `delta` is 0
        then always ends the snapshot.
        """
        kv = await self.get_bucket(bucket)
        watcher = await kv.watch(
            key_pattern,
            include_history=include_history,
            ignore_deletes=ignore_deletes,
            meta_only=meta_only,
            inactive_threshold=inactive_threshold,
        )
        try:
            yield watcher
        finally:
            await watcher.stop()

    def watchall(
        self,
        bucket: str,
        *,
        include_history: bool = False,
        ignore_deletes: bool = False,
        meta_only: bool = False,
        inactive_threshold: float | None = None,
    ) -> AbstractAsyncContextManager[KeyValue.KeyWatcher]:
        """Watch every key with the same context lifetime and options as watch()."""
        return self.watch(
            bucket,
            ">",
            include_history=include_history,
            ignore_deletes=ignore_deletes,
            meta_only=meta_only,
            inactive_threshold=inactive_threshold,
        )

    async def put(
        self,
        bucket: str,
        key: str,
        value: Any,
        revision: int | None = None,
    ) -> int:
        """Store a value in the specified KV bucket.

        Automatically serializes Pydantic models, JSON types, strings, and bytes.
        If revision is specified, uses optimistic concurrency checking (update with last revision).
        Returns the new revision integer.
        """
        kv = await self.get_bucket(bucket)
        payload = serialize_value(value)
        if revision is not None:
            return int(await kv.update(key, payload, last=revision))
        return int(await kv.put(key, payload))

    async def get[T](
        self,
        bucket: str,
        key: str,
        as_type: type[T] | None = None,
        default: Any = None,
        *,
        revision: int | None = None,
    ) -> T | Any:
        """Retrieve a value from the specified KV bucket.

        If the key does not exist or has been deleted, returns default. A key stored with an empty
        value exists and reads back as "" (or b"" with as_type=bytes), not as default.
        Automatically deserializes into as_type (e.g. Pydantic model). With no as_type the text is
        read as JSON when it is valid JSON and as a string when it is not, so a string such as
        "123" or "null" comes back as 123 or None; as_type=str returns exactly what was stored,
        and as_type=dict or as_type=list raises ValueError if the stored JSON is another type.
        """
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
        ):
            raise ValueError("Revision must be a positive integer")
        kv = await self.get_bucket(bucket)
        try:
            entry = await kv.get(key, revision=revision)
        except (
            nats.js.errors.KeyNotFoundError,
            nats.js.errors.KeyDeletedError,
        ):
            await _require_backing_stream(kv)
            return default

        if entry is None:
            return default
        # An empty value is a key that exists: nats-py reports its payload as None, and reading
        # that as the key's absence made "" indistinguishable from a missing key.
        value = getattr(entry, "value", None)
        if value is None:
            value = b""
        return deserialize_value(value, as_type=as_type, default=default)

    async def delete(self, bucket: str, key: str, last: int | None = None) -> None:
        """Write a delete marker for the key, which removes its earlier revisions.

        The marker is written whether or not the key exists: the broker is not asked first, so
        deleting a missing key succeeds and leaves a marker behind, and nothing is returned to say
        which happened. With `last`, a stale revision raises the native
        `nats.js.errors.BadRequestError` (`err_code` 10071). `last` is a revision, so it is a
        positive integer or `None`: nats-py applies the check only to a positive number, and a
        `0` or a negative one would delete with no check at all, which is not what a caller who
        passed a revision asked for; it raises `ValueError` as `get(revision=)` does.
        """
        if last is not None and (isinstance(last, bool) or not isinstance(last, int) or last < 1):
            raise ValueError("Revision must be a positive integer")
        kv = await self.get_bucket(bucket)
        await kv.delete(key, last=last)

    async def purge(
        self, bucket: str, key: str, *, ttl: float | int | timedelta | None = None
    ) -> None:
        """Write a purge marker for the key, which removes all of its revisions.

        As with `delete()`, the marker is written whether or not the key exists and nothing is
        returned to say which happened. `ttl` makes the marker expire (see the README).
        """
        kv = await self.get_bucket(bucket)
        msg_ttl = await self._message_ttl(kv, ttl)
        await kv.purge(key, msg_ttl=msg_ttl)

    async def keys(self, bucket: str, filters: list[str] | None = None) -> list[str]:
        """List all active keys in the specified KV bucket.

        Returns an empty list if the bucket held no active key when read. A key
        is kept when any filter is a substring of it. Raises
        `nats.errors.TimeoutError` if the bucket holds messages and the watch
        delivers none within the JetStream context's timeout.
        """
        kv = await self.get_bucket(bucket)
        entries = await _snapshot(kv, ">", include_history=False, meta_only=True)
        return [
            e.key
            for e in entries
            if e.operation not in (KV_DEL, KV_PURGE)
            and (not filters or any(f in e.key for f in filters))
        ]

    async def history(self, bucket: str, key: str) -> list[Any]:
        """Retrieve revision history entries for a key in the specified KV bucket.

        Returns an empty list if the bucket held no message for the key when
        read. Raises `nats.errors.TimeoutError` if it holds some and the watch
        delivers none within the JetStream context's timeout.
        """
        kv = await self.get_bucket(bucket)
        return await _snapshot(kv, key, include_history=True, meta_only=False)

    # -- Object Store API ------------------------------------------------------

    async def put_object(
        self,
        bucket: str,
        name: str,
        data: bytes | str | Any,
        meta: Any = None,
    ) -> Any:
        """Store an object in the specified Object Store."""
        store = await self.get_object_store(bucket)
        # Bytes and a stream go to nats-py as they are; anything else is stored the way `put()`
        # stores it, and a value with no JSON form raises the same `TypeError` naming its type.
        if not (
            isinstance(data, bytes) or isinstance(data, io.IOBase) or hasattr(data, "readinto")
        ):
            data = serialize_value(data)
        return await store.put(name, data, meta=meta)

    async def get_object(
        self,
        bucket: str,
        name: str,
        writeinto: Any = None,
        show_deleted: bool = False,
    ) -> Any:
        """Retrieve an object from the specified Object Store.

        Returns ObjectResult (with .data and .info) or None if not found.
        """
        store = await self.get_object_store(bucket)
        try:
            return await store.get(name, writeinto=writeinto, show_deleted=show_deleted)
        except nats.js.errors.ObjectNotFoundError:
            await _require_backing_stream(store)
            return None

    async def delete_object(self, bucket: str, name: str) -> Any:
        """Delete an object from the specified Object Store."""
        store = await self.get_object_store(bucket)
        try:
            return await store.delete(name)
        except nats.js.errors.ObjectNotFoundError:
            await _require_backing_stream(store)
            return None

    async def list_objects(
        self,
        bucket: str,
        ignore_deletes: bool = False,
    ) -> list[Any]:
        """List objects in the specified Object Store.

        Returns an empty list if the store contains no objects.
        """
        store = await self.get_object_store(bucket)
        try:
            return cast(list[Any], await store.list(ignore_deletes=ignore_deletes))
        except (nats.js.errors.NotFoundError, nats.js.errors.ObjectNotFoundError):
            await _require_backing_stream(store)
            return []
