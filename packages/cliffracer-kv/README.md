# cliffracer-kv

NATS JetStream Key-Value and Object Store integration extension for Cliffracer services.

## Overview

`cliffracer-kv` provides NATS Key-Value and Object Store integration for Cliffracer services:

- **Service Extension (`KvExtension`)**: Declaratively bound to `self.kv` on your service.
- **Bucket Auto-Provisioning**: Automatic creation of KV buckets and Object Stores with configurable options.
- **Bucket-Level TTL**: Direct support for bucket-level TTL configurations (`ttl` parameter passed to JetStream bucket creation).
- **Serialization**: Automatic serialization and deserialization for Pydantic models, JSON dictionaries, lists, strings, and raw bytes.
- **Optimistic Concurrency**: Supports revision-based updates and optimistic concurrency control.
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
