"""The one way the framework dials a broker under a wall-clock bound.

`nats.connect` builds its client inside the call, so a dial the bound cuts off leaves the caller
holding nothing: the half-open client, and the socket it opened before the broker answered, stay
referenced only by the cancelled call until the next garbage collection. Dialling through a client
the caller owns lets the cut close it.
"""

import asyncio
from typing import Any

import nats
from nats.aio.client import Client

#: Seconds a cut-off client is given to close before the dial reports its own failure.
CLOSE_GRACE = 2.0

#: The connection callbacks nats-py takes. They report a connection the caller was given, so none
#: of them runs for the client a failed dial closes: that connection never existed.
CALLBACKS = ("error_cb", "disconnected_cb", "reconnected_cb", "closed_cb", "discovered_server_cb")

# Close tasks the caller stopped waiting for, held until they finish.
_closing: set["asyncio.Task[None]"] = set()


async def connect(url: str, *, timeout: float | None, **options: Any) -> Client:
    """Connect a client to `url`, giving up after `timeout` seconds (`None` for no bound).

    Whatever ends the dial early, a timeout, a refusal or a cancellation, closes the client it
    opened before the failure leaves this function, so no socket outlives the dial. The callbacks
    do not run for that close: a dial that failed has no connection to report closing.
    """
    client = nats.NATS()
    failed = False

    def withheld_once_failed(callback: Any) -> Any:
        async def run(*args: Any) -> None:
            if not failed:
                await callback(*args)

        return run

    for name in CALLBACKS:
        if options.get(name) is not None:
            options[name] = withheld_once_failed(options[name])
    try:
        if timeout is None:
            await client.connect(url, **options)
        else:
            await asyncio.wait_for(client.connect(url, **options), timeout)
    except BaseException:
        failed = True
        closing = asyncio.get_running_loop().create_task(_close_quietly(client))
        _closing.add(closing)
        closing.add_done_callback(_closing.discard)
        await asyncio.wait({closing}, timeout=CLOSE_GRACE)
        raise
    return client


async def _close_quietly(client: Client) -> None:
    try:
        await client.close()
    except Exception:  # noqa: BLE001 - the dial's own failure is what the caller must see
        pass
