"""Preserve placement when a native KV configuration becomes a stream."""

from copy import copy
from typing import Any

from nats.js.client import JetStreamContext


async def create_bucket(context: Any, **params: Any) -> Any:
    """Carry placement into the initial create request on an isolated context.

    The native KV constructor owns stream defaults and validation. Its stream
    creation call receives placement explicitly, so no bucket is first created
    in an arbitrary cluster and then moved. Shared contexts remain unchanged.
    """
    placement = params.get("placement")
    if placement is None or not issubclass(type(context), JetStreamContext):
        return await context.create_key_value(**params)
    scoped = copy(context)

    async def add_stream(config: Any = None, **options: Any) -> Any:
        return await context.add_stream(config, **{**options, "placement": placement})

    scoped.add_stream = add_stream
    bucket = await scoped.create_key_value(**params)
    bucket._js = context
    return bucket
