"""A client generated from a live `describe` builds a default only when it is the service's own value.

`{service}.describe` answers with the description written with sorted keys, so the keys of a default
reach the generator in an order the class never had. A schema-shape check on the order let a model
whose two aliases are each other's field names (`Swapped`) through, built, over `--nats-url` and not
over `--class`. The service now says which defaults can be rebuilt, having tried it on its own
models, and the generator does what it says.

This runs a real service on a real broker, generates the client over the broker, imports it, and
calls the service with the generated client's own signature check on (`verify=True`, the default),
which compares the hashes the generator wrote with the ones the service computes now.
"""

import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

from cliffracer import ServiceConfig
from cliffracer.core.exceptions import RpcValidationError
from cliffracer.generate_client.cli import main
from cliffracer.introspect import canonical, describe
from tests.fixtures.model_defaults import Live

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

SERVICE = "live_defaults_e2e"


def _import(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _client_of(module):
    for value in vars(module).values():
        if (
            inspect.isclass(value)
            and value.__name__.endswith("Client")
            and value.__module__ == module.__name__
        ):
            return value
    raise AssertionError("no client class")


@pytest.fixture
async def live():
    service = Live(ServiceConfig(name=SERVICE, version="1", health_listener=False))
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


@pytest.mark.filterwarnings("ignore")
async def test_the_live_describe_builds_what_the_service_holds_and_nothing_else(
    live, nats_connection, tmp_path
):
    over_the_broker = tmp_path / "over_the_broker.py"
    from_the_class = tmp_path / "from_the_class.py"
    url = nats_connection.connected_url.geturl()

    code = await asyncio.to_thread(
        main, ["--service", SERVICE, "--nats-url", url, "--out", str(over_the_broker)]
    )
    assert code == 0
    code = await asyncio.to_thread(
        main,
        [
            "--class",
            "tests.fixtures.model_defaults:Live",
            "--service",
            SERVICE,
            "--version",
            "1",
            "--out",
            str(from_the_class),
        ],
    )
    assert code == 0

    source = over_the_broker.read_text()
    # The case the previous check got wrong: the sorted keys of the wire look like the schema's.
    assert 'value: Swapped = {"a": 1, "b": 2}' in source
    assert "Swapped.model_validate" not in source
    # What the service's own models rebuild is built; what they change is the dict.
    assert "value: Household = Household.model_validate(" in source
    assert "value: Extra = Extra.model_validate(" in source
    assert "value: StrictModel = StrictModel.model_validate(" in source
    for kept in ("Secret", "Changed", "Doubled", "Whole", "Jsoned", "Appends", "AddsOne"):
        assert f"{kept}.model_validate" not in source, kept
    assert "Base64Out.model_validate" not in source
    assert "Base64Both.model_validate" in source

    description = describe(Live, service=SERVICE, version="1")
    reached: dict[str, set[str]] = {}
    for label, path in (("over the broker", over_the_broker), ("from the class", from_the_class)):
        module = _import(path, f"live_{label.replace(' ', '_')}")
        client = _client_of(module)(nats_connection)
        original = client._call
        sent: dict[str, dict] = {}

        async def tee(method, params, return_type, original=original, sent=sent):
            sent[method] = params
            return await original(method, params, return_type)

        client._call = tee
        for method in description.methods:
            refused_here = False
            try:
                await getattr(client, method.name)()
                reached.setdefault(label, set()).add(method.name)
            except RpcValidationError as error:
                # The dict default fails the client's own check, as it does on main: nothing is
                # sent, and the default is not one the service said it could rebuild.
                refused_here = str(error).startswith("refused before sending")
                assert refused_here or method.name in sent, (label, method.name, error)
                assert not refused_here or all(p.rebuildable is not True for p in method.params)
            except Exception as error:
                # The service may refuse a payload its own model cannot take back (a masked secret,
                # a type-changing serializer): the payload is what is pinned, and it was sent.
                assert method.name in sent, (label, method.name, error)
            real = inspect.signature(getattr(Live, method.name)).parameters
            generated = inspect.signature(getattr(type(client), method.name)).parameters
            for param in method.params:
                if refused_here:
                    continue
                default = generated[param.name].default
                where = (label, method.name, param.name)
                if param.rebuildable is True and param.default is not None:
                    assert default == real[param.name].default, where
                else:
                    assert canonical(default) == canonical(param.default), where
                assert canonical(sent[method.name][param.name]) == canonical(param.default), where

    for label, methods in reached.items():
        # Calls that went through the generated client to the live service and were answered,
        # built defaults among them. A strict model's service refuses its own JSON dump (it
        # validates strictly), so those calls are sent and refused, as for any caller.
        assert {"household", "extra", "bare_extra", "swapped", "adds_one"} <= methods, label
