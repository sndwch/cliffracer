"""Interpret server expiry markers consistently with native KV tombstones."""

from copy import copy
from dataclasses import replace
from typing import Any, cast

from nats.aio.client import ServerVersion
from nats.js.api import RawStreamMsg
from nats.js.client import JetStreamContext
from nats.js.kv import KV_DEL, KV_MARKER_REASON, KV_OP, KV_PURGE, KeyValue

from .errors import BucketConfigError


class _MarkerAwareJetStream:
    """Delegate JetStream operations, translating only headers on KV reads."""

    def __init__(self, context: JetStreamContext) -> None:
        self._context = context

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    async def get_msg(self, *args: Any, **kwargs: Any) -> RawStreamMsg:
        message = await self._context.get_msg(*args, **kwargs)
        headers = message.headers or {}
        if KV_OP in headers:
            return message
        reason = headers.get(KV_MARKER_REASON, "")
        operation = {"MaxAge": KV_PURGE, "Purge": KV_PURGE, "Remove": KV_DEL}.get(reason)
        if operation is None:
            return message
        return replace(message, headers={**headers, KV_OP: operation})


def expiry_aware_bucket(bucket: Any) -> Any:
    """Give a native handle an isolated read adapter without changing its client.

    Native get/create keep their own key validation, errors, CAS retries and
    message TTL handling. The copied handle alone sees translated headers;
    other users of the JetStream context still receive the original response.
    """
    if not issubclass(type(bucket), KeyValue):
        return bucket
    adapted = copy(cast(KeyValue, bucket))
    adapted._js = cast(JetStreamContext, _MarkerAwareJetStream(adapted._js))
    return adapted


def require_expiry_markers(context: Any) -> None:
    """Refuse connections below the marker notification support floor.

    Every JetStream node must meet the floor, including during rolling upgrades;
    the connected node's version is only the locally observable prerequisite.
    """
    version = getattr(getattr(context, "_nc", None), "connected_server_version", None)
    if not isinstance(version, ServerVersion):
        raise BucketConfigError("Cannot verify the NATS Server version for expiry markers")
    if (version.major, version.minor, version.patch) < (2, 11, 2) or version.prerelease:
        raise BucketConfigError("Expiry markers require NATS Server 2.11.2+ on all JetStream nodes")
