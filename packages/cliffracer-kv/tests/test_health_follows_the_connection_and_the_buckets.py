"""/health reads the client and the buckets, not a flag the extension set once.

`connected` was `self._js is not None`: a JetStream context is a wrapper that does not track the
connection, so it stayed true after the client closed. A probe that asks the broker about every
open bucket and store now makes the service's readiness follow them.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cliffracer_kv.extension as extension_module
import nats.js.errors
import pytest
from cliffracer_kv import JetStreamUnavailableError, KvExtension

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.dependencies import check_dependencies, failed_dependencies

pytestmark = pytest.mark.unit


class _ContextWithNoClient:
    """A JetStream context that names no client, as a stand-in might."""

    async def key_value(self, name):
        return AsyncMock()

    async def object_store(self, name):
        return AsyncMock()


def _js(*, connected: bool = True) -> AsyncMock:
    js = AsyncMock()
    js._nc = SimpleNamespace(is_connected=connected)
    return js


# --- `connected` reads the client ------------------------------------------------


@pytest.mark.asyncio
async def test_connected_is_false_when_the_client_is_disconnected_though_the_context_exists():
    js = _js()
    ext = KvExtension(buckets=["cache"], js=js)
    await ext.start()
    assert ext.health_details()["connected"] is True

    js._nc.is_connected = False

    assert ext.health_details()["connected"] is False


@pytest.mark.asyncio
async def test_connected_follows_the_client_back_when_it_reconnects():
    js = _js(connected=False)
    ext = KvExtension(buckets=["cache"], js=js)
    await ext.start()
    assert ext.health_details()["connected"] is False

    js._nc.is_connected = True

    assert ext.health_details()["connected"] is True


@pytest.mark.asyncio
async def test_connected_reads_an_explicit_connection_when_the_context_names_none():
    nc = SimpleNamespace(is_connected=False)
    ext = KvExtension(buckets=["cache"], nc=nc, js=_ContextWithNoClient())
    await ext.start()

    assert ext.health_details()["connected"] is False


@pytest.mark.asyncio
async def test_CONTROL_with_no_client_to_ask_the_context_existing_is_all_there_is():
    ext = KvExtension(buckets=["cache"], js=_ContextWithNoClient())
    await ext.start()

    assert ext.health_details()["connected"] is True
    await ext.stop()
    assert ext.health_details()["connected"] is False


# --- the probe ---------------------------------------------------------------


def _service_with(js: AsyncMock, **kwargs) -> CliffracerService:
    class Shop(CliffracerService):
        kv = KvExtension(buckets=["cache"], object_stores=["blobs"], js=js, **kwargs)

    return Shop(ServiceConfig(name="shop", health_port=0))


async def _results(service: CliffracerService) -> dict:
    return await check_dependencies(service._dependencies, service.config)


@pytest.mark.asyncio
async def test_starting_the_extension_registers_a_dependency_named_for_it_with_a_bound():
    service = _service_with(_js())
    await service.container._setup_extensions()
    await service.kv.start()

    declared = {dep.name: dep for dep in service._dependencies}

    assert declared["kv"].timeout == extension_module.HEALTH_PROBE_TIMEOUT == 2.0


@pytest.mark.asyncio
async def test_the_probe_passes_while_every_open_bucket_and_store_answers():
    service = _service_with(_js())
    await service.container._setup_extensions()
    await service.kv.start()

    results = await _results(service)

    assert results["kv"]["ok"] is True
    assert failed_dependencies(results) == []


@pytest.mark.asyncio
async def test_a_bucket_that_no_longer_answers_fails_the_probe():
    js = _js()
    service = _service_with(js)
    await service.container._setup_extensions()
    await service.kv.start()
    bucket = await service.kv.get_bucket("cache")
    bucket.status.side_effect = nats.js.errors.NotFoundError()

    results = await _results(service)

    assert failed_dependencies(results) == ["kv"]


@pytest.mark.asyncio
async def test_an_object_store_that_no_longer_answers_fails_the_probe():
    service = _service_with(_js())
    await service.container._setup_extensions()
    await service.kv.start()
    store = await service.kv.get_object_store("blobs")
    store.status.side_effect = nats.js.errors.NotFoundError()

    assert failed_dependencies(await _results(service)) == ["kv"]


@pytest.mark.asyncio
async def test_a_closed_connection_fails_the_probe_before_any_bucket_is_asked():
    js = _js()
    service = _service_with(js)
    await service.container._setup_extensions()
    await service.kv.start()
    bucket = await service.kv.get_bucket("cache")
    js._nc.is_connected = False

    results = await _results(service)

    assert failed_dependencies(results) == ["kv"]
    bucket.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_bucket_that_never_answers_fails_the_probe_within_its_bound(monkeypatch):
    monkeypatch.setattr(extension_module, "HEALTH_PROBE_TIMEOUT", 0.05)
    service = _service_with(_js())
    await service.container._setup_extensions()
    await service.kv.start()
    bucket = await service.kv.get_bucket("cache")

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(30)

    bucket.status.side_effect = hang

    async with asyncio.timeout(5):
        results = await _results(service)

    assert failed_dependencies(results) == ["kv"]


@pytest.mark.asyncio
async def test_a_stopped_extension_fails_the_probe():
    service = _service_with(_js())
    await service.container._setup_extensions()
    await service.kv.start()
    await service.kv.stop()

    assert failed_dependencies(await _results(service)) == ["kv"]


@pytest.mark.asyncio
async def test_two_extensions_on_one_service_each_have_their_own_dependency():
    js = _js()

    class Two(CliffracerService):
        orders = KvExtension(buckets=["orders"], js=js)
        stock = KvExtension(buckets=["stock"], js=js)

    service = Two(ServiceConfig(name="two", health_port=0))
    await service.container._setup_extensions()
    await service.orders.start()
    await service.stock.start()

    assert sorted(dep.name for dep in service._dependencies) == ["orders", "stock"]


@pytest.mark.asyncio
async def test_CONTROL_an_extension_with_no_service_registers_nothing_and_starts():
    ext = KvExtension(buckets=["cache"], js=_js())

    await ext.start()

    assert ext.health_details()["buckets"] == ["cache"]


@pytest.mark.asyncio
async def test_the_probe_names_what_is_wrong_when_called_directly():
    js = _js(connected=False)
    ext = KvExtension(buckets=["cache"], js=js)
    await ext.start()

    with pytest.raises(JetStreamUnavailableError, match="NATS connection"):
        await ext._probe()
