"""An extension can react to the broker connection being lost and regained.

The `Extension` interface had no disconnect or reconnect hook, and the container's connection
callbacks reached exactly one place, the single `ServiceConfig.on_disconnect` / `on_connect` slot.
An extension that had to react (drop what it was routing, unsubscribe a subject before the client
replays it) had to take the slot the user may be using. `on_disconnect` and `on_reconnect` are hooks
like the others: run by the container from its connection callbacks in declaration order, guarded
(a hook that raises is logged and the rest still run), and the config slot still runs after them.

The hooks run inside the client's own callbacks. The client awaits the disconnect one before it
reconnects, so one that waits delays the reconnect; the docs say to be quick or hand off.
"""

from __future__ import annotations

import asyncio

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit


def _recorder(name: str, calls: list[str], *, boom: str | None = None, sync: bool = False):
    class Rec(Extension):
        if sync:

            def on_disconnect(self) -> None:  # type: ignore[override]
                calls.append(f"{name}.on_disconnect")
                if boom == "on_disconnect":
                    raise RuntimeError("disconnect hook failed")

            def on_reconnect(self) -> None:  # type: ignore[override]
                calls.append(f"{name}.on_reconnect")
                if boom == "on_reconnect":
                    raise RuntimeError("reconnect hook failed")

        else:

            async def on_disconnect(self) -> None:
                calls.append(f"{name}.on_disconnect")
                if boom == "on_disconnect":
                    raise RuntimeError("disconnect hook failed")

            async def on_reconnect(self) -> None:
                calls.append(f"{name}.on_reconnect")
                if boom == "on_reconnect":
                    raise RuntimeError("reconnect hook failed")

    return Rec()


def _service(calls: list[str], extensions: dict[str, Extension], **config):
    attrs = dict(extensions)
    service_class = type("Svc", (CliffracerService,), attrs)
    return service_class(
        ServiceConfig(
            name="hooks_svc",
            health_port=0,
            on_disconnect=lambda: calls.append("config.on_disconnect"),
            on_connect=lambda: calls.append("config.on_connect"),
            **config,
        )
    )


async def test_the_hooks_run_in_declaration_order_and_the_config_slot_runs_after_them():
    calls: list[str] = []
    svc = _service(
        calls, {"first": _recorder("first", calls), "second": _recorder("second", calls)}
    )

    await svc.container.connection._disconnected_callback()
    await svc.container.connection._reconnected_callback()

    assert calls == [
        "first.on_disconnect",
        "second.on_disconnect",
        "config.on_disconnect",
        "first.on_reconnect",
        "second.on_reconnect",
        "config.on_connect",
    ]


@pytest.mark.parametrize("sync", [False, True], ids=["async-hook", "sync-hook"])
@pytest.mark.parametrize("hook", ["on_disconnect", "on_reconnect"])
async def test_a_hook_that_raises_is_logged_and_the_rest_still_run(hook, sync):
    calls: list[str] = []
    svc = _service(
        calls,
        {
            "bad": _recorder("bad", calls, boom=hook, sync=sync),
            "good": _recorder("good", calls, sync=sync),
        },
    )
    method = (
        svc.container.connection._disconnected_callback
        if hook == "on_disconnect"
        else svc.container.connection._reconnected_callback
    )

    await method()

    assert calls[:2] == [f"bad.{hook}", f"good.{hook}"]
    assert calls[2] == ("config.on_disconnect" if hook == "on_disconnect" else "config.on_connect")


async def test_a_cancelled_hook_is_not_swallowed():
    class Slow(Extension):
        async def on_disconnect(self) -> None:
            await asyncio.sleep(30)

    svc = _service([], {"slow": Slow()})
    task = asyncio.create_task(svc.container.connection._disconnected_callback())
    await asyncio.sleep(0.05)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_default_hooks_do_nothing_and_an_extension_without_them_is_unaffected():
    calls: list[str] = []
    svc = _service(calls, {"plain": Extension()})

    await svc.container.connection._disconnected_callback()
    await svc.container.connection._reconnected_callback()

    assert calls == ["config.on_disconnect", "config.on_connect"]


async def test_a_service_with_no_config_slot_still_runs_the_hooks():
    calls: list[str] = []
    service_class = type("Svc", (CliffracerService,), {"ext": _recorder("ext", calls)})
    svc = service_class(ServiceConfig(name="hooks_svc", health_port=0))

    await svc.container.connection._disconnected_callback()
    await svc.container.connection._reconnected_callback()

    assert calls == ["ext.on_disconnect", "ext.on_reconnect"]


async def test_an_extension_added_after_construction_is_heard_too():
    calls: list[str] = []
    svc = _service(calls, {})
    svc.add_extension(_recorder("late", calls), name="late")

    await svc.container.connection._disconnected_callback()

    assert calls[0] == "late.on_disconnect"
