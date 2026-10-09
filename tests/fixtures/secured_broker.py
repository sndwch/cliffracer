"""Disposable brokers with real role permissions and deterministic cleanup."""

import asyncio
import json
import subprocess
import uuid

import nats
import pytest


@pytest.fixture(params=["nats:2.10.29-alpine", "nats:2.11.2-alpine"])
async def secured_broker(tmp_path, request):
    containers, connections, services = [], [], []
    errors = {}

    async def start(roles):
        users = [{"user": "inspector", "password": "inspection-only"}]
        users.extend(
            {"user": name, "password": "disposable", "permissions": policy.to_nats_permissions()}
            for name, policy in roles.items()
        )
        config = tmp_path / "nats.json"
        config.write_text(
            json.dumps(
                {
                    "port": 4222,
                    "jetstream": {"store_dir": "/tmp/jetstream"},
                    "authorization": {"users": users},
                }
            )
        )
        name = "cliffracer-permissions-" + uuid.uuid4().hex
        container = subprocess.run(
            [
                "docker",
                "create",
                "--name",
                name,
                "-p",
                "127.0.0.1::4222",
                request.param,
                "-c",
                "/tmp/permissions.json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        ).stdout.strip()
        containers.append(container)
        subprocess.run(
            ["docker", "cp", str(config), container + ":/tmp/permissions.json"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        subprocess.run(["docker", "start", container], check=True, capture_output=True, timeout=30)
        address = subprocess.run(
            ["docker", "port", container, "4222/tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        assert address.startswith("127.0.0.1:"), address
        url = "nats://" + address
        inspector = nats.NATS()
        connections.append(inspector)
        async with asyncio.timeout(10):
            await inspector.connect(
                url,
                user="inspector",
                password="inspection-only",
                connect_timeout=0.2,
                max_reconnect_attempts=30,
                reconnect_time_wait=0.05,
            )
        return url, inspector

    async def connect(url, role, *, inbox_prefix):
        errors[role] = asyncio.Queue()
        nc = await nats.connect(
            url,
            user=role,
            password="disposable",
            inbox_prefix=inbox_prefix,
            error_cb=errors[role].put,
            allow_reconnect=False,
        )
        connections.append(nc)
        return nc

    async def serve(cls, config, url, role):
        errors[role] = asyncio.Queue()
        config = config.model_copy(
            update={
                "nats_url": url,
                "nats_user": role,
                "nats_password": "disposable",
                "on_error": errors[role].put,
            }
        )
        service = cls(config)
        services.append(service)
        await service.start()
        return service

    yield start, connect, serve, errors
    for service in reversed(services):
        await service.stop()
    for connection in reversed(connections):
        await connection.close()
    for container in reversed(containers):
        subprocess.run(
            ["docker", "rm", "-f", container], check=True, capture_output=True, timeout=30
        )
