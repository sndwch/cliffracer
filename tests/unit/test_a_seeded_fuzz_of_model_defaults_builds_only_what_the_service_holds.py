"""A seeded fuzz: a generated client builds a default only when it is the service's own value.

Random pydantic models, from a fixed seed, each used as the default of an RPC parameter: scalars
that dump to another JSON type (datetime, UUID, Decimal, bytes, enums), containers, nested models,
unions whose JSON form fits more than one member, aliases (including two fields whose aliases are
each other's names), `extra="allow"`, strict models, secrets, base64 bytes, and field serializers
and validators that change a type or are not idempotent. Each goes through the whole path, over both
ways a generator gets a description (the class in process, and the sorted-key bytes of `describe`):
describe, emit, import. The two clients are the same bytes. Then, for every default:

- the client imports;
- a default the client built is equal to the value the service holds;
- the payload of a call that leaves the argument out is the described dump, byte for byte
  (canonical JSON), built or not.

The fuzz also asserts that it reached what it claims to: models the service called rebuildable and
models it did not, and each hazard at least a few times, so a generator that stopped producing one
fails here instead of passing quietly.
"""

import asyncio
import datetime
import decimal
import enum
import importlib.util
import inspect
import json
import random
import sys
import types
import uuid
import warnings
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    field_serializer,
    field_validator,
)

from cliffracer import CliffracerService, rpc
from cliffracer.core.exceptions import RpcValidationError
from cliffracer.generate_client.emitter import emit
from cliffracer.introspect import Description, canonical, describe
from tests.fixtures.model_defaults import Cat, Dog

pytestmark = pytest.mark.unit

SEED = 20261003
MODELS = 300
MODULE = "cliffracer_fuzz_models"


class Color(enum.Enum):
    red = "red"
    blue = "blue"


class Maker:
    """Builds one random model class, its default instance and a note of the hazards in it."""

    def __init__(self, rng: random.Random, index: int, module: types.ModuleType) -> None:
        self.rng = rng
        self.index = index
        self.module = module
        self.hazards: Counter[str] = Counter()
        self.count = 0

    # A field: (annotation, how to make a value). Values are made in the shape the model takes.
    def scalar(self) -> tuple[Any, Any]:
        r = self.rng
        choice = r.choice(
            [
                "int", "float", "str", "bool", "decimal", "datetime", "date", "uuid", "bytes",
                "enum", "literal", "secret", "set", "tuple",
            ]
        )  # fmt: skip
        if choice == "int":
            return int, r.randint(-5, 50)
        if choice == "float":
            return float, r.choice([0.5, 1.0, 2.25, -3.0])
        if choice == "str":
            return str, r.choice(["a", "b c", "", "é"])
        if choice == "bool":
            return bool, r.choice([True, False])
        if choice == "decimal":
            self.hazards["decimal"] += 1
            return decimal.Decimal, decimal.Decimal(r.choice(["1.5", "0.1", "10", "1E+2"]))
        if choice == "datetime":
            self.hazards["datetime"] += 1
            return datetime.datetime, datetime.datetime(2026, r.randint(1, 12), 1, r.choice([0, 3]))
        if choice == "date":
            return datetime.date, datetime.date(2026, r.randint(1, 12), 1)
        if choice == "uuid":
            return uuid.UUID, uuid.UUID(int=r.randint(1, 99))
        if choice == "bytes":
            return bytes, r.choice([b"x", b"hello", b""])
        if choice == "enum":
            return Color, r.choice(list(Color))
        if choice == "literal":
            return Literal["a", "b"], r.choice(["a", "b"])
        if choice == "secret":
            self.hazards["secret"] += 1
            return SecretStr, SecretStr(r.choice(["s", "hunter2"]))
        if choice == "set":
            return set[int], {r.randint(1, 3)}
        return tuple[int, str], (r.randint(0, 3), "t")

    def union(self) -> tuple[Any, Any]:
        r = self.rng
        self.hazards["union"] += 1
        choice = r.choice(
            ["int_str", "date_datetime", "float_decimal", "str_uuid", "pets", "list_tuple"]
        )
        if choice == "int_str":
            return int | str, r.choice([1, "1", "x"])
        if choice == "date_datetime":
            self.hazards["date_datetime"] += 1
            return datetime.date | datetime.datetime, r.choice(
                [
                    datetime.date(2026, 1, 1),
                    datetime.datetime(2026, 1, 1),
                    datetime.datetime(2026, 1, 1, 5),
                ]
            )
        if choice == "float_decimal":
            self.hazards["float_decimal"] += 1
            return float | decimal.Decimal, r.choice(
                [0.5, decimal.Decimal("0.1"), decimal.Decimal("2")]
            )
        if choice == "str_uuid":
            return str | uuid.UUID, r.choice(["x", uuid.UUID(int=3)])
        if choice == "list_tuple":
            return list[int] | tuple[int, ...], r.choice([[1, 2], (1, 2)])
        return Cat | Dog, r.choice([Cat(lives=3), Dog(good=False)])

    def field(self, depth: int) -> tuple[Any, Any]:
        r = self.rng
        kind = r.choices(
            ["scalar", "union", "optional", "list", "dict", "nested"],
            weights=[8, 2, 2, 2, 1, 2 if depth < 2 else 0],
        )[0]
        if kind == "scalar":
            return self.scalar()
        if kind == "union":
            return self.union()
        inner_annotation, inner_value = self.scalar() if kind != "nested" else (None, None)
        if kind == "optional":
            return inner_annotation | None, r.choice([None, inner_value])
        if kind == "list":
            return list[inner_annotation], [inner_value]  # type: ignore[valid-type]
        if kind == "dict":
            return dict[str, inner_annotation], {"k": inner_value}  # type: ignore[valid-type]
        model, instance = self.model(depth + 1)
        return model, instance

    def model(self, depth: int = 0) -> tuple[type[BaseModel], BaseModel]:
        r = self.rng
        self.count += 1
        name = f"M{self.index}_{self.count}"
        names = [f"f{i}" for i in range(r.randint(1, 4))]
        annotations: dict[str, Any] = {}
        namespace: dict[str, Any] = {}
        values: dict[str, Any] = {}
        config: dict[str, Any] = {}
        if r.random() < 0.25:
            config["strict"] = True
            self.hazards["strict"] += 1
        extra = r.choice(["ignore", "ignore", "ignore", "allow", "forbid"])
        if extra != "ignore":
            config["extra"] = extra
        if extra == "allow":
            self.hazards["extra_allow"] += 1
        if r.random() < 0.3:
            config["populate_by_name"] = True
        if r.random() < 0.1:
            config["ser_json_bytes"] = "base64"
            self.hazards["base64"] += 1
            if r.random() < 0.5:
                config["val_json_bytes"] = "base64"

        aliases: dict[str, str] = {}
        swap = len(names) >= 2 and r.random() < 0.12
        if swap:
            a, b = names[0], names[1]
            aliases[a], aliases[b] = b, a
            self.hazards["swap"] += 1
        else:
            for n in names:
                if r.random() < 0.15:
                    aliases[n] = n.upper()
                    self.hazards["alias"] += 1

        for n in names:
            annotation, value = self.field(depth)
            hazard = r.random()
            if hazard < 0.2 and annotation in (int, str, float):
                ser = r.choice(["type_change", "non_idempotent"])
                self.hazards["serializer"] += 1
                if ser == "type_change":
                    namespace[f"_ser_{n}"] = field_serializer(n)(lambda self, v: f"#{v}")
                elif annotation is int:
                    annotation = Annotated[int, PlainSerializer(lambda v: v * 2, return_type=int)]
                else:
                    namespace[f"_ser_{n}"] = field_serializer(n)(lambda self, v: v)
            elif hazard < 0.4 and annotation is str:
                self.hazards["validator"] += 1
                namespace[f"_val_{n}"] = field_validator(n)(classmethod(lambda cls, v: v + "!"))
            annotations[n] = annotation
            namespace[n] = Field(alias=aliases[n]) if n in aliases else Field()
            values[n] = value

        namespace["__annotations__"] = annotations
        namespace["model_config"] = ConfigDict(**config)
        namespace["__module__"] = MODULE
        model = type(name, (BaseModel,), namespace)
        setattr(self.module, name, model)

        by_alias = {aliases.get(n, n): values[n] for n in names}
        if extra == "allow" and r.random() < 0.6:
            by_alias["stray"] = r.choice([1, "s", (1, 2)])
        try:
            instance = model.model_validate(by_alias)
        except Exception:
            instance = model.model_construct(**{n: values[n] for n in names})
        return model, instance


def _service_of(model: type[BaseModel], default: BaseModel) -> type:
    async def handler(self, value: model = default) -> int:  # type: ignore[valid-type]
        return 0

    return type("FuzzService", (CliffracerService,), {"m": rpc(handler)})


def _import(source: str, tmp_path: Path, name: str):
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
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


def _generation(rng_seed: int, tmp_path: Path):
    """Yield (index, model, default, hazards, description) for each model of the run."""
    rng = random.Random(rng_seed)
    module = types.ModuleType(MODULE)
    sys.modules[MODULE] = module
    try:
        for index in range(MODELS):
            maker = Maker(rng, index, module)
            model, default = maker.model()
            yield index, model, default, maker.hazards
    finally:
        sys.modules.pop(MODULE, None)


@pytest.mark.filterwarnings("ignore")
def test_a_default_is_built_only_when_it_is_the_services_value_and_every_payload_is_the_dump(
    tmp_path,
):
    seen: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for index, model, default, hazards in _generation(SEED, tmp_path):
            service = _service_of(model, default)
            try:
                in_process = describe(service, service="fuzz", version="1")
            except Exception:
                outcomes["not described"] += 1  # a model pydantic has no JSON Schema for
                continue
            param = in_process.methods[0].params[0]
            seen.update(hazards)
            outcomes[f"flag {param.rebuildable}"] += 1
            on_the_wire = Description.from_dict(json.loads(canonical(in_process.to_dict())))
            assert emit(in_process) == emit(on_the_wire), (index, model.__name__, hazards)
            for path, description in (("class", in_process), ("wire", on_the_wire)):
                module = _import(emit(description), tmp_path, f"fuzz_{index}_{path}")
                client = _client_of(module)(nats_url="nats://broker.invalid:6999", verify=False)
                sent: dict[str, Any] = {}

                async def record(method, params, return_type, sent=sent):
                    sent[method] = params
                    return 0

                client._call = record
                where = (index, path, model.__name__, hazards)
                generated = inspect.signature(client.m).parameters["value"].default
                built = not isinstance(generated, dict)
                try:
                    asyncio.run(client.m())
                except RpcValidationError:
                    # The dict default fails the client's own check, as it does on main.
                    assert not built, where
                    assert canonical(generated) == canonical(param.default), where
                    outcomes["refused locally"] += 1
                    continue
                if built:
                    outcomes["built"] += 1
                    assert param.rebuildable is True, where
                    assert generated == default, where
                else:
                    outcomes["kept as the dict"] += 1
                assert canonical(sent["m"]["value"]) == canonical(param.default), where

    # The run reached what it claims to.
    assert outcomes["flag True"] >= 60 and outcomes["flag False"] >= 60, outcomes
    kept = outcomes["kept as the dict"] + outcomes["refused locally"]
    assert outcomes["built"] >= 120 and kept >= 120, outcomes
    assert outcomes["kept as the dict"] >= 40, outcomes  # kept, and the call goes through
    for hazard in (
        "swap", "alias", "extra_allow", "union", "date_datetime", "float_decimal", "secret",
        "serializer", "validator", "strict", "decimal", "datetime", "base64",
    ):  # fmt: skip
        assert seen[hazard] >= 5, (hazard, seen)
