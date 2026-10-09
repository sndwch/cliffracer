"""A KV watch read as a client that has not caught up with the server.

nats-py's `KeyValue.watch` queues its end marker first when the consumer reports nothing pending
and the subscription reports nothing received. Under load both hold while the entries are sent but
still unread. `lagging_watches(kv)` forces that state on each watch subscription `kv` makes while
it is open: `consumer_info()` is read only once the server reports nothing pending, and the
received count reads 0. Nothing else is patched, and the patch is removed on exit.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager


class LaggingCount:
    """One watch subscription, read as a client that has not caught up with the server."""

    def __init__(self, sub) -> None:
        self._real = sub
        self.forced = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    @property
    def delivered(self) -> int:
        return 0

    async def consumer_info(self):
        for _ in range(100):
            info = await self._real.consumer_info()
            if info.num_pending == 0 and self._real.delivered > 0:
                self.forced = True
                return info
            await asyncio.sleep(0.02)
        return info


@contextmanager
def lagging_watches(kv) -> Iterator[list[LaggingCount]]:
    """Wrap each subscription `kv` makes while open; restore the context after."""
    js = kv._js
    real_subscribe = js.subscribe
    made: list[LaggingCount] = []

    async def subscribe(*args, **kwargs):
        lagging = LaggingCount(await real_subscribe(*args, **kwargs))
        made.append(lagging)
        return lagging

    js.subscribe = subscribe
    try:
        yield made
    finally:
        del js.subscribe
