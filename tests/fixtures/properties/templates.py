"""Template settings models a service template is registered with, and an oracle for each document.

`generate(rng, index)` builds one settings model (by `create_model`) and an instance of it. Its fields
hold stdlib dataclasses, models and pydantic dataclasses, each under one of the alias configs (an
`alias_generator`, `populate_by_name`, `validate_by_alias=False`), directly or in a list, a tuple, a
dict, a set or frozenset of frozen dataclasses, or a `RootModel`; a model field may carry an explicit
`AliasChoices`, a one-key `AliasPath` or a `serialization_alias`; a dataclass may have a field with
`init=False`; a model may write a field through a serializer that changes it, writes a different
value that then reads back as itself (`abs` of -1, a clamp, rounding) or drops a list's last item;
a model may allow extras, which are planted (a scalar, now and then a set of dataclasses); and a model may hold a field with one allowed value read
through `AliasChoices` or a one-key `AliasPath`. About half the
instances have their scalars changed from the defaults.

`reads_back(model, instance)` is the oracle: the document pydantic writes (`model_dump_json`, by
alias, round-trip) validates to the instance, every scalar changed on its own in the instance is
read back changed, and so is every field of every item of a set, changed on its own. A settings
model is reconstructed from that document alone, so a value that is not read back from it is lost.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import random
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    RootModel,
    WrapSerializer,
    create_model,
    field_serializer,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


CONFIGS: list[ConfigDict | None] = [
    None,
    ConfigDict(alias_generator=camel, populate_by_name=True),
    ConfigDict(alias_generator=camel),
    ConfigDict(alias_generator=camel, validate_by_alias=False, validate_by_name=True),
    ConfigDict(alias_generator=lambda name: name.upper(), populate_by_name=True),
    ConfigDict(extra="allow"),
    ConfigDict(alias_generator=camel, populate_by_name=True, extra="allow"),
]

#: Serializers that write a different value, which reads back as itself: the copy a model's own
#: dump makes already holds it, so only a comparison with the caller's value sees the change.
STABLE_AND_LOSSY: list[tuple[Any, Any, Any]] = [
    (int, PlainSerializer(abs), -1),
    (int, PlainSerializer(lambda v: max(0, min(v, 10))), 20),
    (float, PlainSerializer(lambda v: round(v, 1)), 1.25),
]


#: A draw that picks each kind of root field once: a holder, a leaf, a list, a tuple, a dict, a set,
#: a frozenset, a model holding one field with one allowed value, and a root model, in the order the
#: draw's thresholds give them.
SINGLE_ROOTS = [0.1, 0.4, 0.6, 0.7, 0.75, 0.85, 0.9, 0.96, 0.99]


class _Builder:
    def __init__(self, rng: random.Random, index: int) -> None:
        self.rng = rng
        self.index = index
        self.count = 0

    def name(self, prefix: str) -> str:
        self.count += 1
        return f"{prefix}{self.index}_{self.count}"

    def scalar(self) -> tuple[type, Any]:
        kind = self.rng.choice([int, str, bool])
        value = {
            int: lambda: self.rng.randint(-50, 50),
            str: lambda: self.rng.choice(["a", "bb", "q z", ""]),
            bool: lambda: self.rng.random() < 0.5,
        }[kind]()
        return kind, value

    def leaf(self, frozen: bool = False) -> type:
        """A stdlib dataclass of scalars with multi-word names, some with `init=False`."""
        fields: list[tuple[str, type, Any]] = []
        names: set[str] = set()
        for k in range(self.rng.randint(1, 4)):
            name = f"box_f{k}_{self.rng.choice(['x', 'yy'])}"
            if name in names:
                continue
            names.add(name)
            kind, value = self.scalar()
            init = not (not frozen and self.rng.random() < 0.08)
            fields.append((name, kind, dataclasses.field(default=value, init=init)))
        return dataclasses.make_dataclass(self.name("L"), fields, frozen=frozen)

    def config(self, leaves: list[type]) -> ConfigDict | None:
        """One of the configs. Pydantic refuses `extra="allow"` over a dataclass with an
        `init=False` field, so a config allowing extras is not chosen over one."""
        if any(not item.init for leaf in leaves for item in dataclasses.fields(leaf)):
            return self.rng.choice([c for c in CONFIGS if not (c and c.get("extra") == "allow")])
        return self.rng.choice(CONFIGS)

    def holder(self, leaves: list[type]) -> type:
        """A model or a pydantic dataclass under one of the configs, holding leaves."""
        config = self.config(leaves)
        held = {f"h_f{k}_v": self.rng.choice(leaves) for k in range(self.rng.randint(1, 3))}
        if self.rng.random() < 0.5:
            fields: dict[str, Any] = {}
            for name, leaf in held.items():
                options: dict[str, Any] = {"default_factory": leaf}
                r = self.rng.random()
                if r < 0.1:
                    options["validation_alias"] = AliasChoices(name + "_c", name)
                elif r < 0.3:  # written under its name, read only from another key
                    options["validation_alias"] = AliasPath(name + "_p")
                elif r < 0.4:
                    options["serialization_alias"] = name + "_s"
                fields[name] = (leaf, Field(**options))
            r = self.rng.random()
            if r < 0.1:  # a serializer that writes a different value, which then reads back stable
                kind, serializer, value = self.rng.choice(STABLE_AND_LOSSY)
                fields["count"] = (Annotated[kind, serializer], value)
            elif r < 0.4:  # one value only, so the probe cannot try another: the schema decides
                # More often than not it is written under a key the schema does not read.
                out = {"serialization_alias": "one_out"} if self.rng.random() < 0.6 else {}
                if r < 0.25:
                    reads: Any = AliasChoices("tag_c", "tag")
                    fields["tag"] = (Literal["a"], Field("a", validation_alias=reads, **out))
                else:
                    fields["mark"] = (
                        Literal["m"],
                        Field("m", validation_alias=AliasPath("mark"), **out),
                    )
            base: type[BaseModel] = BaseModel
            r = self.rng.random()
            if r < 0.1:
                base = _lossy_base(next(iter(held)))
            elif r < 0.2:
                fields["items"] = (list[int], [1, 2, 3])
                base = _dropping_base("items")
            kwargs: dict[str, Any] = {"__base__": base}
            if config is not None and base is BaseModel:
                kwargs = {"__config__": config}
            return create_model(self.name("H"), **kwargs, **fields)
        namespace = {
            "__annotations__": dict(held),
            **{name: dataclasses.field(default_factory=leaf) for name, leaf in held.items()},
        }
        cls = type(self.name("PH"), (), namespace)
        return pydantic_dataclass(cls, config=config) if config else pydantic_dataclass(cls)

    def one_value(self) -> type[BaseModel]:
        """A model holding one field with one allowed value, read through `AliasChoices` or a one-key
        `AliasPath`, more often than not written under a key it is not read from."""
        out = {"serialization_alias": "one_out"} if self.rng.random() < 0.6 else {}
        if self.rng.random() < 0.5:
            reads: Any = AliasChoices("tag_c", "tag")
            return create_model(
                self.name("Tag"), tag=(Literal["a"], Field("a", validation_alias=reads, **out))
            )
        return create_model(
            self.name("Mark"),
            mark=(Literal["m"], Field("m", validation_alias=AliasPath("mark"), **out)),
        )

    def settings_model(self) -> type[BaseModel]:
        leaves = [self.leaf() for _ in range(self.rng.randint(1, 3))]
        if self.rng.random() < 0.4:  # a dataclass holding one leaf twice
            leaf = self.rng.choice(leaves)
            leaves.append(
                dataclasses.make_dataclass(
                    self.name("M"),
                    [
                        ("box_leaf", leaf, dataclasses.field(default_factory=leaf)),
                        ("other_leaf", leaf, dataclasses.field(default_factory=leaf)),
                    ],
                )
            )
        frozen = self.leaf(frozen=True)
        roots: dict[str, Any] = {}
        # One root field often, so a model's outcome turns on that one field's route, and then each
        # kind of root is equally likely.
        single = self.rng.random() < 0.4
        for k in range(1 if single else self.rng.randint(2, 5)):
            leaf = self.rng.choice(leaves)
            r = self.rng.random() if not single else self.rng.choice(SINGLE_ROOTS)
            if r < 0.35:
                kind: Any = self.holder(leaves)
                default: Any = kind
            elif r < 0.55:
                kind, default = leaf, leaf
            elif r < 0.65:
                kind, default = list[leaf], lambda leaf=leaf: [leaf(), leaf()]  # type: ignore[valid-type]
            elif r < 0.72:
                kind, default = tuple[leaf, leaf], lambda leaf=leaf: (leaf(), leaf())  # type: ignore[valid-type]
            elif r < 0.8:
                kind, default = dict[str, leaf], lambda leaf=leaf: {"k_one": leaf()}  # type: ignore[valid-type]
            elif r < 0.875:
                kind, default = set[frozen], lambda: {frozen()}  # type: ignore[valid-type]
            elif r < 0.95:
                kind, default = frozenset[frozen], lambda: frozenset({frozen()})  # type: ignore[valid-type]
            elif r < 0.97:
                kind = default = self.one_value()
            else:
                root = RootModel[list[leaf]]  # type: ignore[valid-type]
                kind, default = root, lambda root=root, leaf=leaf: root([leaf()])
            roots[f"r{k}"] = (kind, Field(default_factory=default))
        config = self.config([*leaves, frozen])
        return create_model(
            self.name("Root"), **({"__config__": config} if config else {}), **roots
        )


def _lossy_base(field_name: str) -> type[BaseModel]:
    """A model base whose serializer writes `field_name` changed: such a model cannot round-trip."""

    class Lossy(BaseModel):
        @field_serializer(field_name, check_fields=False)
        def _changed(self, value: Any) -> Any:
            return None

    return Lossy


def _dropping_base(field_name: str) -> type[BaseModel]:
    """A model base whose serializer writes the list `field_name` without its last item."""

    class Dropping(BaseModel):
        @field_serializer(field_name, check_fields=False)
        def _shorter(self, value: list[Any]) -> list[Any]:
            return value[:-1]

    return Dropping


#: Fields that allow one value only, which are never changed.
ONE_VALUE = frozenset({"tag", "mark"})


def scalar_paths(value: Any, path: tuple[tuple[str, Any], ...] = ()):
    """Each settable scalar in a settings instance, by its path of attributes and positions. A set
    has no position, so nothing below one is changed."""
    if isinstance(value, RootModel):
        yield from scalar_paths(value.root, (*path, ("attr", "root")))
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            if name not in ONE_VALUE:
                yield from scalar_paths(getattr(value, name), (*path, ("attr", name)))
        for key, item in (value.model_extra or {}).items():
            yield from scalar_paths(item, (*path, ("extra", key)))
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        if not type(value).__dataclass_params__.frozen:
            for item in dataclasses.fields(value):
                yield from scalar_paths(getattr(value, item.name), (*path, ("attr", item.name)))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from scalar_paths(item, (*path, ("item", index)))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from scalar_paths(item, (*path, ("item", key)))
    elif isinstance(value, bool | int | float | str):
        yield path, value


def _at(value: Any, path: tuple[tuple[str, Any], ...]) -> Any:
    for kind, step in path:
        if kind == "attr":
            value = getattr(value, step)
        elif kind == "extra":
            value = value.model_extra[step]
        else:
            value = value[step]
    return value


def _set(value: Any, path: tuple[tuple[str, Any], ...], new: Any) -> None:
    holder = _at(value, path[:-1])
    kind, step = path[-1]
    if kind == "attr":
        object.__setattr__(holder, step, new)
    elif kind == "extra":
        holder.__pydantic_extra__[step] = new
    else:
        holder[step] = new


def other(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value + 7
    return value + "q"


def generate(rng: random.Random, index: int) -> tuple[type[BaseModel], BaseModel]:
    """One settings model and an instance of it, its scalars changed from the defaults about half
    the time."""
    model = _Builder(rng, index).settings_model()
    instance = model()
    _plant_extras(rng, instance)
    if rng.random() < 0.5:
        for path, value in list(scalar_paths(instance)):
            if rng.random() < 0.6:
                _set(instance, path, other(value))
    return model, instance


def _plant_extras(rng: random.Random, value: Any) -> None:
    """An extra on each model in `value` that allows them: a scalar, or now and then a frozenset of
    frozen dataclasses, which no type reads back."""
    if isinstance(value, BaseModel) and not isinstance(value, RootModel):
        if type(value).model_config.get("extra") == "allow":
            extra: Any = rng.choice([1, "keep-me", True])
            if rng.random() < 0.1:
                boxed = dataclasses.make_dataclass("Boxed", [("n", int, 1)], frozen=True)
                extra = frozenset({boxed()})
            value.__pydantic_extra__["note_x"] = extra
        for name in type(value).model_fields:
            _plant_extras(rng, getattr(value, name))


def set_variants(value: Any, path: tuple[tuple[str, Any], ...] = ()):
    """For each item of each set or frozenset in a settings instance, and each of its fields, the
    path to the set, the set with that item's field changed, and the changed item."""
    if isinstance(value, RootModel):
        yield from set_variants(value.root, (*path, ("attr", "root")))
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from set_variants(getattr(value, name), (*path, ("attr", name)))
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for item in dataclasses.fields(value):
            yield from set_variants(getattr(value, item.name), (*path, ("attr", item.name)))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from set_variants(item, (*path, ("item", index)))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from set_variants(item, (*path, ("item", key)))
    elif isinstance(value, set | frozenset):
        for member in value:
            if dataclasses.is_dataclass(member):
                for item in dataclasses.fields(member):
                    changed = dataclasses.replace(
                        member, **{item.name: other(getattr(member, item.name))}
                    )
                    yield path, type(value)((value - {member}) | {changed}), changed


def _document(instance: BaseModel) -> Any:
    return json.loads(instance.model_dump_json(by_alias=True, round_trip=True))


def _changes(value: Any) -> list[Any]:
    """Values to put in place of `value`: `other`, then a number below it, since a serializer that
    clamps can write every larger number as the same one."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return [other(value), value - 7]
    return [other(value)]


def _writes_itself(instance: BaseModel, path: tuple[tuple[str, Any], ...]) -> bool:
    """Whether the scalar at `path` is a model field with its own serializer, which may write
    several values as one."""
    kind, name = path[-1]
    holder = _at(instance, path[:-1])
    field = type(holder).model_fields.get(name) if isinstance(holder, BaseModel) else None
    return (
        kind == "attr"
        and field is not None
        and any(isinstance(item, PlainSerializer | WrapSerializer) for item in field.metadata)
    )


def reads_back(model: type[BaseModel], instance: BaseModel) -> tuple[bool, str]:
    """Whether the document pydantic writes for `instance` is read back as it, and every scalar
    changed on its own in the document is read back changed; and if not, why not.

    A scalar is changed in the instance and written again, and must read back as the change. A
    change that a field's own serializer writes as the same document asks nothing of the key, so the
    next one is tried, and such a field no change reaches the document of is not asked about. Any
    other change the document does not carry is a value lost."""
    try:
        document = _document(instance)
        before = model.model_validate(document)
        if before != instance:
            return False, "the document reads back as other values"
        for path, value in scalar_paths(instance):
            for candidate in _changes(value):
                changed = copy.deepcopy(instance)
                _set(changed, path, candidate)
                written = _document(changed)
                if written == document and _writes_itself(instance, path):
                    continue
                if _at(model.model_validate(written), path) != candidate:
                    return False, f"a change at {path} is not read back"
                break
        for path, changed_set, member in set_variants(instance):
            changed = copy.deepcopy(instance)
            _set(changed, path, changed_set)
            if member not in _at(model.model_validate(_document(changed)), path):
                return False, f"a change to an item of the set at {path} is not read back"
    except Exception as exc:
        return False, f"{type(exc).__name__} reading the document back"
    return True, ""
