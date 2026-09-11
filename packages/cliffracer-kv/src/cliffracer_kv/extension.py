"""NATS JetStream Key-Value and Object Store extension."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, cast

import nats.js.errors

from cliffracer.core.extension import Extension, ExtensionSetupContext

from .config import BucketConfig, ObjectStoreConfig, normalize_ttl_seconds
from .errors import JetStreamUnavailableError
from .serialization import deserialize_value, serialize_value


class KvExtension(Extension):
    """JetStream Key-Value and Object Store client extension.

    Bound as a class attribute to provide KV bucket and Object Store handle
    caches, serialization, and automated bucket provisioning during service setup.
    """

    name: str = "kv"

    def __init__(
        self,
        buckets: Sequence[str | BucketConfig | dict[str, Any]] | None = None,
        bucket_ttls: Mapping[str, float | int | timedelta] | None = None,
        object_stores: Sequence[str | ObjectStoreConfig | dict[str, Any]] | None = None,
        object_store_ttls: Mapping[str, float | int | timedelta] | None = None,
        create_if_missing: bool = True,
        nc: Any = None,
        js: Any = None,
    ) -> None:
        # DECLARED here, CREATED in setup(). bind() is a shallow copy, so
        # mutable dictionaries built in __init__ would be shared across bound
        # instances.
        self._init_buckets = tuple(buckets) if buckets else ()
        self._init_bucket_ttls = dict(bucket_ttls) if bucket_ttls else {}
        self._init_object_stores = tuple(object_stores) if object_stores else ()
        self._init_object_store_ttls = dict(object_store_ttls) if object_store_ttls else {}
        self.create_if_missing = create_if_missing
        self._explicit_nc = nc
        self._explicit_js = js

        self._kv_stores: dict[str, Any] | None = None
        self._obj_stores: dict[str, Any] | None = None
        self._bucket_configs: dict[str, BucketConfig] | None = None
        self._object_store_configs: dict[str, ObjectStoreConfig] | None = None
        self._js: Any = None

    # -- lifecycle -------------------------------------------------------------

    async def setup(self, ctx: ExtensionSetupContext) -> None:
        """Run before broker connect: build per-instance config tables and caches."""
        self._kv_stores = {}
        self._obj_stores = {}
        self._bucket_configs = {}
        self._object_store_configs = {}

        # 1. Configured buckets from constructor
        for b in self._init_buckets:
            name = (
                b
                if isinstance(b, str)
                else (b.name if isinstance(b, BucketConfig) else (b.get("name") or b.get("bucket")))
            )
            default_ttl = self._init_bucket_ttls.get(str(name)) if name is not None else None
            cfg = BucketConfig.from_value(b, default_ttl=default_ttl)
            self._bucket_configs[cfg.name] = cfg

        # 2. Configured bucket TTLs without explicit bucket entry
        for name, ttl in self._init_bucket_ttls.items():
            if name not in self._bucket_configs:
                self._bucket_configs[name] = BucketConfig(name=name, ttl=ttl)

        # 3. Dynamic service_config support if declared on ServiceConfig
        if ctx is not None and getattr(ctx, "service_config", None) is not None:
            kv_buckets = getattr(ctx.service_config, "kv_buckets", None)
            if kv_buckets:
                for b in kv_buckets:
                    name = (
                        b
                        if isinstance(b, str)
                        else (
                            b.name
                            if isinstance(b, BucketConfig)
                            else (b.get("name") or b.get("bucket"))
                        )
                    )
                    default_ttl = (
                        self._init_bucket_ttls.get(str(name)) if name is not None else None
                    )
                    cfg = BucketConfig.from_value(b, default_ttl=default_ttl)
                    if cfg.name not in self._bucket_configs:
                        self._bucket_configs[cfg.name] = cfg

        # 4. Configured object stores
        for o in self._init_object_stores:
            name = (
                o
                if isinstance(o, str)
                else (
                    o.name
                    if isinstance(o, ObjectStoreConfig)
                    else (o.get("name") or o.get("bucket"))
                )
            )
            default_ttl = self._init_object_store_ttls.get(str(name)) if name is not None else None
            os_cfg = ObjectStoreConfig.from_value(o, default_ttl=default_ttl)
            self._object_store_configs[os_cfg.name] = os_cfg

        for name, ttl in self._init_object_store_ttls.items():
            if name not in self._object_store_configs:
                self._object_store_configs[name] = ObjectStoreConfig(name=name, ttl=ttl)

    async def start(self) -> None:
        """Run after broker connect: resolve JetStream and auto-provision stores."""
        self._ensure_initialized()
        self._js = self._resolve_js()

        # Auto-provision declared buckets
        assert self._bucket_configs is not None
        for cfg in self._bucket_configs.values():
            await self._ensure_bucket(cfg)

        # Auto-provision declared object stores
        assert self._object_store_configs is not None
        for os_cfg in self._object_store_configs.values():
            await self._ensure_object_store(os_cfg)

    async def stop(self) -> None:
        """Run on shutdown: clear cached handles and reset state."""
        if self._kv_stores is not None:
            self._kv_stores.clear()
        if self._obj_stores is not None:
            self._obj_stores.clear()
        self._js = None

    def health_details(self) -> dict[str, Any] | None:
        """Report active buckets and object stores on /health."""
        return {
            "connected": self._js is not None,
            "buckets": list(self._kv_stores.keys()) if self._kv_stores else [],
            "object_stores": list(self._obj_stores.keys()) if self._obj_stores else [],
        }

    # -- internal helpers ------------------------------------------------------

    def _ensure_initialized(self) -> None:
        """Ensure per-instance structures exist even if setup() was bypassed."""
        if self._kv_stores is None:
            self._kv_stores = {}
            self._obj_stores = {}
            self._bucket_configs = {}
            self._object_store_configs = {}

            for b in self._init_buckets:
                name = (
                    b
                    if isinstance(b, str)
                    else (
                        b.name
                        if isinstance(b, BucketConfig)
                        else (b.get("name") or b.get("bucket"))
                    )
                )
                default_ttl = self._init_bucket_ttls.get(str(name)) if name is not None else None
                cfg = BucketConfig.from_value(b, default_ttl=default_ttl)
                self._bucket_configs[cfg.name] = cfg

            for name, ttl in self._init_bucket_ttls.items():
                if name not in self._bucket_configs:
                    self._bucket_configs[name] = BucketConfig(name=name, ttl=ttl)

            for o in self._init_object_stores:
                name = (
                    o
                    if isinstance(o, str)
                    else (
                        o.name
                        if isinstance(o, ObjectStoreConfig)
                        else (o.get("name") or o.get("bucket"))
                    )
                )
                default_ttl = (
                    self._init_object_store_ttls.get(str(name)) if name is not None else None
                )
                os_cfg = ObjectStoreConfig.from_value(o, default_ttl=default_ttl)
                self._object_store_configs[os_cfg.name] = os_cfg

            for name, ttl in self._init_object_store_ttls.items():
                if name not in self._object_store_configs:
                    self._object_store_configs[name] = ObjectStoreConfig(name=name, ttl=ttl)

    def _resolve_js(self) -> Any:
        """Resolve the active NATS JetStream context."""
        if self._js is not None:
            return self._js
        if self._explicit_js is not None:
            return self._explicit_js

        # Try service.js
        if self.service is not None and getattr(self.service, "js", None) is not None:
            return self.service.js

        # Try explicit_nc.jetstream()
        if self._explicit_nc is not None:
            try:
                return self._explicit_nc.jetstream()
            except Exception as err:
                raise JetStreamUnavailableError(
                    f"Failed to create JetStream context from explicit NATS connection: {err}"
                ) from err

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
            "JetStream is required for KvExtension but no JetStream context "
            "or active NATS connection was found on the service. "
            "Ensure jetstream_enabled=True in ServiceConfig or inject js."
        )

    @property
    def js(self) -> Any:
        """Direct access to the underlying JetStream context."""
        return self._resolve_js()

    async def _ensure_bucket(self, config: BucketConfig) -> Any:
        """Retrieve existing KeyValue handle or create it with configured options (including TTL)."""
        js = self._resolve_js()
        self._js = js

        try:
            kv = await js.key_value(config.name)
            assert self._kv_stores is not None
            self._kv_stores[config.name] = kv
            return kv
        except Exception as exc:
            is_not_found = (
                isinstance(
                    exc,
                    nats.js.errors.BucketNotFoundError | nats.js.errors.NotFoundError,
                )
                or "bucket not found" in str(exc).lower()
                or "stream not found" in str(exc).lower()
            )
            if not is_not_found or not self.create_if_missing:
                raise

            # Auto-provision bucket with bucket-level TTL configuration
            params: dict[str, Any] = {"bucket": config.name}
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

            kv = await js.create_key_value(**params)
            assert self._kv_stores is not None
            self._kv_stores[config.name] = kv
            return kv

    async def _ensure_object_store(self, config: ObjectStoreConfig) -> Any:
        """Retrieve existing ObjectStore handle or create it with configured options."""
        js = self._resolve_js()
        self._js = js

        try:
            store = await js.object_store(config.name)
            assert self._obj_stores is not None
            self._obj_stores[config.name] = store
            return store
        except Exception as exc:
            is_not_found = (
                isinstance(
                    exc,
                    nats.js.errors.ObjectNotFoundError
                    | nats.js.errors.BucketNotFoundError
                    | nats.js.errors.NotFoundError,
                )
                or "not found" in str(exc).lower()
            )
            if not is_not_found or not self.create_if_missing:
                raise

            params: dict[str, Any] = {"bucket": config.name}
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

    async def get_bucket(self, bucket: str) -> Any:
        """Get or auto-provision a KeyValue bucket handle."""
        self._ensure_initialized()
        assert self._kv_stores is not None
        if bucket in self._kv_stores:
            return self._kv_stores[bucket]

        assert self._bucket_configs is not None
        cfg = self._bucket_configs.get(bucket)
        if cfg is None:
            ttl = self._init_bucket_ttls.get(bucket)
            cfg = BucketConfig(name=bucket, ttl=ttl)
            self._bucket_configs[bucket] = cfg

        return await self._ensure_bucket(cfg)

    async def get_object_store(self, bucket: str) -> Any:
        """Get or auto-provision an ObjectStore handle."""
        self._ensure_initialized()
        assert self._obj_stores is not None
        if bucket in self._obj_stores:
            return self._obj_stores[bucket]

        assert self._object_store_configs is not None
        cfg = self._object_store_configs.get(bucket)
        if cfg is None:
            ttl = self._init_object_store_ttls.get(bucket)
            cfg = ObjectStoreConfig(name=bucket, ttl=ttl)
            self._object_store_configs[bucket] = cfg

        return await self._ensure_object_store(cfg)

    # -- Key-Value API ---------------------------------------------------------

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
    ) -> T | Any:
        """Retrieve a value from the specified KV bucket.

        If the key does not exist or has been deleted, returns default.
        Automatically deserializes into as_type (e.g. Pydantic model) or inferred JSON/str/bytes.
        """
        kv = await self.get_bucket(bucket)
        try:
            entry = await kv.get(key)
        except (
            nats.js.errors.KeyNotFoundError,
            nats.js.errors.KeyDeletedError,
            nats.js.errors.NotFoundError,
        ):
            return default
        except Exception as exc:
            if "key not found" in str(exc).lower() or "deleted" in str(exc).lower():
                return default
            raise

        if entry is None or getattr(entry, "value", None) is None:
            return default
        if getattr(entry, "operation", None) == "DEL":
            return default

        return deserialize_value(entry.value, as_type=as_type, default=default)

    async def delete(self, bucket: str, key: str, last: int | None = None) -> bool:
        """Delete a key from the specified KV bucket.

        Returns True if deleted, False if key was not found.
        """
        kv = await self.get_bucket(bucket)
        try:
            await kv.delete(key, last=last)
            return True
        except (
            nats.js.errors.KeyNotFoundError,
            nats.js.errors.KeyDeletedError,
            nats.js.errors.NotFoundError,
        ):
            return False
        except Exception as exc:
            if "key not found" in str(exc).lower():
                return False
            raise

    async def purge(self, bucket: str, key: str) -> bool:
        """Purge all revisions of a key from the specified KV bucket.

        Returns True if purged, False if key was not found.
        """
        kv = await self.get_bucket(bucket)
        try:
            await kv.purge(key)
            return True
        except (
            nats.js.errors.KeyNotFoundError,
            nats.js.errors.KeyDeletedError,
            nats.js.errors.NotFoundError,
        ):
            return False
        except Exception as exc:
            if "key not found" in str(exc).lower():
                return False
            raise

    async def keys(self, bucket: str, filters: list[str] | None = None) -> list[str]:
        """List all active keys in the specified KV bucket.

        Returns an empty list if the bucket contains no keys.
        """
        kv = await self.get_bucket(bucket)
        try:
            return cast(list[str], await kv.keys(filters=filters))
        except (
            nats.js.errors.NoKeysError,
            nats.js.errors.KeyNotFoundError,
            nats.js.errors.NotFoundError,
        ):
            return []
        except Exception as exc:
            if "no keys" in str(exc).lower() or "key not found" in str(exc).lower():
                return []
            raise

    async def history(self, bucket: str, key: str) -> list[Any]:
        """Retrieve revision history entries for a key in the specified KV bucket.

        Returns an empty list if the key does not exist.
        """
        kv = await self.get_bucket(bucket)
        try:
            return cast(list[Any], await kv.history(key))
        except (
            nats.js.errors.NoKeysError,
            nats.js.errors.KeyNotFoundError,
            nats.js.errors.NotFoundError,
        ):
            return []
        except Exception as exc:
            if "no keys" in str(exc).lower() or "key not found" in str(exc).lower():
                return []
            raise

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
        if isinstance(data, str):
            data = data.encode("utf-8")
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
        except (nats.js.errors.ObjectNotFoundError, nats.js.errors.NotFoundError):
            return None
        except Exception as exc:
            if "not found" in str(exc).lower():
                return None
            raise

    async def delete_object(self, bucket: str, name: str) -> Any:
        """Delete an object from the specified Object Store."""
        store = await self.get_object_store(bucket)
        try:
            return await store.delete(name)
        except (nats.js.errors.ObjectNotFoundError, nats.js.errors.NotFoundError):
            return None
        except Exception as exc:
            if "not found" in str(exc).lower():
                return None
            raise

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
            return []
        except Exception as exc:
            if "not found" in str(exc).lower():
                return []
            raise
