#!/usr/bin/env python3
"""
Two apps, same service name, isolated by namespace — plus a cross-namespace watcher.

Run (requires NATS at nats://localhost:4222):
    python examples/namespaces/namespaced_apps_example.py
"""

import asyncio

from cliffracer import CliffracerService, ServiceConfig, listener, rpc


def make_user_service(namespace: str, health_port: int):
    class UserService(CliffracerService):
        @rpc
        async def whoami(self) -> dict[str, str]:
            return {"namespace": namespace}

    return UserService(
        ServiceConfig(name="user_service", namespace=namespace, health_port=health_port)
    )


class Watcher(CliffracerService):
    def __init__(self, health_port: int = 8012):
        super().__init__(ServiceConfig(name="watcher", namespace="watch", health_port=health_port))
        self.seen: list[str] = []

    @listener("orders.created", cross_namespace=True, fanout=True)
    async def on_order(self, subject: str, n: int = 0) -> None:
        self.seen.append(subject)
        self.logger.info(f"watcher saw {subject}")


async def main():
    a = make_user_service("appA", health_port=8010)
    b = make_user_service("appB", health_port=8011)
    watcher = Watcher(health_port=8012)
    caller = CliffracerService(ServiceConfig(name="caller", namespace="appA", health_port=8013))
    for svc in (a, b, watcher, caller):
        await svc.start()
    await asyncio.sleep(0.2)

    # RPC stays within appA
    who = await caller.call_rpc("user_service", "whoami")
    print(f"caller in appA reached: {who}")

    # broadcasts from both apps reach the cross-namespace watcher
    await a.publish_event("orders.created", n=1)
    await b.publish_event("orders.created", n=2)
    await asyncio.sleep(0.3)
    print(f"watcher saw: {sorted(watcher.seen)}")

    for svc in (a, b, watcher, caller):
        await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
