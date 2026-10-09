"""Registered service factories with frozen RPC contracts and isolated settings."""

from __future__ import annotations

import copy
import inspect
import json
import math
import threading
import weakref
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, is_dataclass
from typing import Any, cast

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    PlainSerializer,
    RootModel,
    TypeAdapter,
    ValidationError,
    WrapSerializer,
)
from pydantic.dataclasses import is_pydantic_dataclass
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from cliffracer.core.extension import Extension
from cliffracer.core.outputs import (
    OutputBindings,
    OutputContract,
    PreparedOutput,
    ResolvedOutput,
    describe_outputs,
    output_declarations,
    prepare_outputs,
)
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.validation import _same
from cliffracer.introspect import canonical, describe

from .contracts import ActivationConflict, RpcContract, TemplateError

_LOST = "settings must round-trip through JSON without changing values"

_constructed: weakref.WeakValueDictionary[int, CliffracerService] = weakref.WeakValueDictionary()
_construction_lock = threading.Lock()


def _validation_inputs(
    config: Mapping[str, Any], name: str, info: FieldInfo
) -> list[str | AliasPath]:
    alias = info.validation_alias or info.alias
    inputs: list[str | AliasPath] = []
    if config.get("validate_by_alias", True) and alias is not None:
        inputs.extend(alias.choices if isinstance(alias, AliasChoices) else [alias])
    if alias is None or config.get(
        "validate_by_name",
        config.get("populate_by_name", False) or not config.get("validate_by_alias", True),
    ):
        inputs.append(name)
    return [
        item.path[0]
        if isinstance(item, AliasPath) and len(item.path) == 1 and isinstance(item.path[0], str)
        else item
        for item in inputs
    ]


def _holds_its_own_config(node: dict[str, Any], holder: type | None) -> bool:
    """Whether a schema node is a model or pydantic dataclass other than the one searched from.

    Such a node validates its fields under its own config, so a stdlib dataclass found inside it is
    read under that config and is not the one the holder's own fields use.
    """
    kind, node_cls = node.get("type"), node.get("cls")
    if node_cls is None or node_cls is holder:
        return False
    return kind == "model" or (
        kind == "dataclass" and isinstance(node_cls, type) and is_pydantic_dataclass(node_cls)
    )


def _dataclass_settings_fields(
    schema: Any, cls: type, holder: type | None = None
) -> dict[str, FieldInfo] | None:
    """Read resolved field aliases without re-evaluating local annotations.

    The search starts at the schema of `holder`, the model or pydantic dataclass whose config
    applies to `cls`, and does not enter another model or pydantic dataclass inside it.
    """
    values: Iterable[Any]
    if isinstance(schema, dict):
        if schema.get("type") == "dataclass" and schema.get("cls") is cls:
            arguments = schema["schema"]
            while arguments["type"] != "dataclass-args":
                arguments = arguments["schema"]
            result = {}
            for item in arguments["fields"]:
                alias = item.get("validation_alias")
                if isinstance(alias, list):
                    alias = (
                        AliasChoices(*(AliasPath(*path) for path in alias))
                        if alias and isinstance(alias[0], list)
                        else AliasPath(*alias)
                    )
                result[item["name"]] = FieldInfo(
                    validation_alias=alias,
                    serialization_alias=item.get("serialization_alias"),
                    init=item.get("init"),
                    init_var=item.get("init_only"),
                )
            return result
        if _holds_its_own_config(schema, holder):
            return None
        values = schema.values()
    elif isinstance(schema, list):
        values = schema
    else:
        return None
    for value in values:
        found = _dataclass_settings_fields(value, cls, holder)
        if found is not None:
            return found
    return None


#: A step from a value to what it holds, as the validated model is walked: an attribute, an item of
#: a list or tuple, a value of a mapping. A set has no stable position, so nothing below one is probed.
_Step = tuple[str, Any]

_ABSENT = object()


def _other_values(value: Any) -> Iterable[Any]:
    """Scalars of the same JSON type as `value`, each different from it, to try in its place."""
    if isinstance(value, bool):
        yield not value
    elif isinstance(value, int):
        yield from (value + 1, value - 1, value + 2)
    elif isinstance(value, float):
        yield from (value + 1.0, value - 1.0, value * 2.0 + 1.0)
    elif isinstance(value, str):
        yield from (value + "x", "x" + value, "x" if value != "x" else "y")


def _scalar_count(document: Any) -> int:
    """How many scalars a JSON document holds."""
    if isinstance(document, dict):
        return sum(_scalar_count(item) for item in document.values())
    if isinstance(document, list):
        return sum(_scalar_count(item) for item in document)
    return 1


def _scalar_paths(written: Any, limit: int = 3) -> list[tuple[tuple[str | int, ...], Any]]:
    """Up to `limit` paths, inside a written value, to a scalar that has a different value."""
    found: list[tuple[tuple[str | int, ...], Any]] = []

    def visit(node: Any, path: tuple[str | int, ...]) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, bool | int | float | str):
            found.append((path, node))
        elif isinstance(node, dict):
            for key, item in node.items():
                visit(item, (*path, key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, (*path, index))

    visit(written, ())
    return found


def _with_value(document: Any, path: tuple[str | int, ...], value: Any) -> Any:
    """A copy of `document` with `value` at `path`.

    Only the containers on the path are copied, one level each; everything else is shared with
    `document`, which is only read. The copy costs the size of those containers, not of the
    whole document.
    """

    def replaced(node: Any, depth: int) -> Any:
        if depth == len(path):
            return value
        copied: Any = dict(node) if isinstance(node, dict) else list(node)
        copied[path[depth]] = replaced(node[path[depth]], depth + 1)
        return copied

    return replaced(document, 0)


def _follow(obj: Any, steps: tuple[_Step, ...]) -> Any:
    for kind, argument in steps:
        obj = getattr(obj, argument) if kind == "attr" else obj[argument]
    return obj


class _Probe:
    """Asks pydantic whether a key is read, by changing the value written under it.

    The schema stored on a model does not always describe what pydantic runs, so where the check is
    about to refuse a field, the document is validated again with a different value at a key of the
    object that holds the field. The key is the field's when three things hold: the change moves that
    field and no other; the changed document dumps back with the change at that same key and at no
    other; and the field set alone to its moved value on the original instance dumps with the change
    at that same key and at no other. A validator that swaps or rotates keys between fields moves the
    field but the dump writes it elsewhere; a field filled from an extra key is written under its own
    key, which the dump of the changed document can hide (a stale extra of the same name, a computed
    field or a serializer writing that name), so the field is also set alone, where nothing else moves.
    Anything that cannot be told is no evidence: an unreadable key, a value no different one can
    replace, a document the model refuses once changed, or a budget spent.
    The budget is per document, `BUDGET` plus `PER_SCALAR` for each scalar it holds and at most
    `MAX_BUDGET`. Each copy of the document costs one unit, and each validation, and each dump of the
    field set alone, one plus one for every `BYTES_PER_UNIT` bytes of the document's JSON, since it
    reads all of them. So the bytes the probe reads in all are bounded whatever the document's size,
    and the check returns as soon as the budget is spent.
    """

    BUDGET = 256
    PER_SCALAR = 8
    MAX_BUDGET = 4096
    #: A validation reads the whole document, so it costs one more unit for each this many bytes.
    BYTES_PER_UNIT = 4096

    def __init__(self, model_type: type[BaseModel], document: Any) -> None:
        self.model_type = model_type
        self.document = document
        text = json.dumps(document)
        self.original = model_type.model_validate_json(text)
        self.budget: float = min(
            self.MAX_BUDGET, self.BUDGET + self.PER_SCALAR * _scalar_count(document)
        )
        self.validation_cost = 1 + len(text) / self.BYTES_PER_UNIT

    def _spend(self, units: float = 1) -> bool:
        """Take `units` of the budget, or say it is spent."""
        if self.budget < units:
            self.budget = 0
            return False
        self.budget -= units
        return True

    def _validated(self, document: Any) -> BaseModel | None:
        try:
            return self.model_type.model_validate_json(json.dumps(document))
        except Exception:
            return None

    def _dumped_at(self, result: BaseModel, document_path: tuple[str | int, ...]) -> Any:
        """What the validated `result` dumps at `document_path`, as the document is written."""
        node = json.loads(result.model_dump_json(round_trip=True, by_alias=True))
        for part in document_path:
            node = node[part]
        return node

    def _set_alone_changes_only(
        self,
        name: str,
        value: Any,
        steps: tuple[_Step, ...],
        document_path: tuple[str | int, ...],
        serialized: dict[str, Any],
        key: str,
    ) -> bool:
        """Whether the original instance, with field `name` alone set to `value`, dumps the change at
        `key` of the object at `document_path` and at no other key of it."""
        copied = self.original.model_copy(deep=True)
        object.__setattr__(_follow(copied, steps), name, value)
        dumped = self._dumped_at(copied, document_path)
        return {
            member
            for member in dumped.keys() | serialized.keys()
            if dumped.get(member, _ABSENT) != serialized.get(member, _ABSENT)
        } == {key}

    def key_read_by(
        self,
        name: str,
        serialized: dict[str, Any],
        document_path: tuple[str | int, ...],
        steps: tuple[_Step, ...] | None,
        fields: tuple[str, ...],
        preferred: Collection[Any] = (),
    ) -> str | None:
        """The key of `serialized` whose change changes field `name` and no other of `fields`.

        A key that also changes a sibling is not evidence for this field: a validator can fill
        one field from another, and the key is then the sibling's. The key the schema expects the
        field under, and the keys it reads (`preferred`), are tried first, so a field costs a
        validation or two and not one per key.
        """
        if steps is None:
            return None
        try:
            before_holder = _follow(self.original, steps)
            before = getattr(before_holder, name)
        except Exception:
            return None
        for key in sorted(serialized, key=lambda candidate: candidate not in preferred):
            for relative, scalar in _scalar_paths(serialized[key]):
                for candidate in _other_values(scalar):
                    if not self._spend():
                        return None
                    try:
                        changed = _with_value(
                            self.document, (*document_path, key, *relative), candidate
                        )
                    except Exception:
                        return None
                    if not self._spend(self.validation_cost):
                        return None
                    result = self._validated(changed)
                    if result is None:
                        continue
                    try:
                        holder = _follow(result, steps)
                        moved = getattr(holder, name) != before
                        others = any(
                            getattr(holder, other) != getattr(before_holder, other)
                            for other in fields
                            if other != name
                        )
                        if moved and not others:
                            dumped = self._dumped_at(result, document_path)
                            carried_here = {
                                member
                                for member in dumped.keys() | serialized.keys()
                                if dumped.get(member, _ABSENT) != serialized.get(member, _ABSENT)
                            } == {key}
                            if carried_here:
                                if not self._spend(self.validation_cost):
                                    return None
                                if self._set_alone_changes_only(
                                    name,
                                    getattr(holder, name),
                                    steps,
                                    document_path,
                                    serialized,
                                    key,
                                ):
                                    return key
                    except Exception:
                        break
                    break
        return None


def _check_settings_inputs(
    value: Any,
    serialized: Any,
    inherited_config: Mapping[str, Any] | None = None,
    schema: Any = None,
    holder: type | None = None,
    probe: _Probe | None = None,
    document_path: tuple[str | int, ...] = (),
    steps: tuple[_Step, ...] | None = (),
) -> None:
    """Require stored model fields to be explicit inputs when reconstructed.

    `probe`, with where `value` sits in the document (`document_path`) and in the validated model
    (`steps`), is how a field whose key the schema does not read is told apart from one pydantic
    does read, so that the schema's reading of a shared definition cannot refuse a valid document.
    """
    config: Mapping[str, Any] = inherited_config or {}

    def below(step: _Step | None) -> tuple[_Step, ...] | None:
        return None if steps is None or step is None else (*steps, step)

    if isinstance(value, BaseModel) or is_pydantic_dataclass(type(value)):
        # A model or a pydantic dataclass validates its fields under its own config, and so does
        # every stdlib dataclass inside it: the aliases of those come from its schema and not from
        # the schema of whichever model happens to hold it.
        schema = type(value).__pydantic_core_schema__
        holder = type(value)
    if isinstance(value, RootModel):
        _check_settings_inputs(
            value.root,
            serialized,
            type(value).model_config,
            schema,
            holder,
            probe,
            document_path,
            below(("attr", "root")),
        )
        return
    if isinstance(value, BaseModel):
        declared = type(value).model_fields
        config = type(value).model_config
    elif is_dataclass(value) and not isinstance(value, type):
        # A stdlib dataclass and a pydantic dataclass alike: the field info of a pydantic dataclass
        # does not carry the aliases its `alias_generator` produces, but its schema does.
        resolved = _dataclass_settings_fields(schema, type(value), holder)
        if resolved is None:
            raise TemplateError("settings dataclass must have a resolved validation schema")
        declared = resolved
        config = getattr(type(value), "__pydantic_config__", config)
    elif isinstance(value, Mapping):
        if isinstance(serialized, dict):
            keys = TypeAdapter(dict[Any, Any], config=cast(ConfigDict, config))
            for key, item in value.items():
                encoded_key = next(iter(keys.dump_python({key: None}, mode="json")))
                _check_settings_inputs(
                    item,
                    serialized.get(encoded_key),
                    config,
                    schema,
                    holder,
                    probe,
                    (*document_path, encoded_key),
                    below(("item", key)),
                )
        return
    elif isinstance(value, list | tuple | set | frozenset):
        if isinstance(serialized, list):
            if len(serialized) != len(value):
                raise TemplateError("settings must round-trip through JSON without changing values")
            ordered = isinstance(value, list | tuple)
            for index, (item, encoded) in enumerate(zip(value, serialized, strict=True)):
                _check_settings_inputs(
                    item,
                    encoded,
                    config,
                    schema,
                    holder,
                    probe,
                    (*document_path, index),
                    below(("item", index) if ordered else None),
                )
        return
    else:
        return
    for name, info in declared.items():
        key = info.serialization_alias or info.alias or name
        if info.init is False or info.init_var:
            raise TemplateError(
                f"settings field {name!r} must round-trip through an accepted serialized key"
            )
        inputs = _validation_inputs(config, name, info)
        if key not in inputs or not isinstance(serialized, dict) or key not in serialized:
            found = (
                probe.key_read_by(
                    name, serialized, document_path, steps, tuple(declared), (key, *inputs)
                )
                if probe is not None and isinstance(serialized, dict)
                else None
            )
            if found is None:
                raise TemplateError(
                    f"settings field {name!r} must round-trip through an accepted serialized key"
                )
            key = found
        _check_settings_inputs(
            getattr(value, name),
            serialized[key],
            config,
            schema,
            holder,
            probe,
            (*document_path, key),
            below(("attr", name)),
        )


def _python_copy(settings: BaseModel) -> Any:
    """The data `normalize` validates for a settings model, in Python types.

    A model is dumped in Python mode, so a retry keeps the validated Python types. A field holding a
    set or frozenset of dataclasses cannot be dumped so, since each item becomes a dict, which does
    not hash. Such a field's value is copied as it is, under the key its dump writes, and every other
    field and extra is dumped as before. A field whose own serializer is what fails is refused: its
    value cannot be copied past the serializer.
    """

    def dump(**kwargs: Any) -> Any:
        return settings.model_dump(mode="python", round_trip=True, by_alias=True, **kwargs)

    try:
        return dump()
    except TypeError:
        if isinstance(settings, RootModel):
            if _serializes_itself(type(settings), "root"):
                raise TemplateError(_LOST) from None
            return settings.root
        fields = type(settings).model_fields
        failing = []
        for name in [*fields, *(settings.model_extra or {})]:
            try:
                dump(include={name})
            except TypeError:
                failing.append(name)
        if not failing:
            raise
        if any(name in fields and _serializes_itself(type(settings), name) for name in failing):
            raise TemplateError(_LOST) from None
        data = dump(exclude=set(failing))
        for name in failing:
            info = fields.get(name)
            key = (info.serialization_alias or info.alias or name) if info else name
            data[key] = getattr(settings, name)
        return data


def _serializes_itself(model: type[BaseModel], name: str) -> bool:
    """Whether field `name` of `model` has a serializer of its own: a `field_serializer`, or a plain
    or wrap serializer in its annotation."""
    decorators = model.__pydantic_decorators__.field_serializers.values()
    if any(name in d.info.fields or "*" in d.info.fields for d in decorators):
        return True
    info = model.model_fields.get(name)
    return info is not None and any(
        isinstance(item, PlainSerializer | WrapSerializer) for item in info.metadata
    )


def _copy_runtime_config(config: ServiceConfig) -> ServiceConfig:
    """Isolate runtime data while preserving the host's explicitly supplied callbacks."""
    callbacks = (config.on_connect, config.on_disconnect, config.on_error)
    data = copy.deepcopy(
        config.model_dump(mode="python", round_trip=True),
        {id(callback): callback for callback in callbacks if callback is not None},
    )
    return ServiceConfig.model_validate(data)


def _check_extension(name: str, extension: Extension) -> None:
    for hook in ("start", "stop"):
        if getattr(type(extension), hook) is not getattr(Extension, hook):
            raise TemplateError(f"unsupported template declaration {name}.{hook}")


def _check_surface(service_class: type[CliffracerService]) -> None:
    declared: dict[str, Extension] = {}
    for owner in reversed(service_class.__mro__):
        for name, value in vars(owner).items():
            if isinstance(value, Extension):
                declared[name] = value
    for name, extension in declared.items():
        _check_extension(name, extension)
    for name, member in inspect.getmembers_static(service_class):
        if name.startswith("_"):
            continue
        if isinstance(member, staticmethod | classmethod):
            member = member.__func__
        markers = getattr(member, "__dict__", {})
        for marker in (
            "_cliffracer_events",
            "_cliffracer_validated_events",
            "_cliffracer_broadcast",
            "_cliffracer_timers",
        ):
            if markers.get(marker):
                raise TemplateError(f"unsupported template declaration {name}: {marker}")


def _extras_read_back(read: Any, given: Any) -> bool:
    """Whether every model in `read` holds exactly the extras of the model at its place in `given`.

    An extra has no declared type to be read back as, so a set or a dataclass in one comes back as a
    list or a dict: not what was given, though the two may compare equal by value (`_same`, which
    compares a model by its declared fields). Models are matched by place: a field, a list or tuple
    item by index, a dict value by key. Anything else holds no model whose extras could differ.
    """
    if isinstance(read, BaseModel) and isinstance(given, BaseModel):
        if (read.model_extra or {}) != (given.model_extra or {}):
            return False
        return all(
            _extras_read_back(getattr(read, name, None), getattr(given, name, None))
            for name in type(given).model_fields
        )
    if isinstance(read, list | tuple) and isinstance(given, list | tuple):
        return len(read) == len(given) and all(
            _extras_read_back(a, b) for a, b in zip(read, given, strict=True)
        )
    if isinstance(read, Mapping) and isinstance(given, Mapping):
        return all(_extras_read_back(read.get(key), item) for key, item in given.items())
    return True


@dataclass(frozen=True)
class NormalizedSettings[S: BaseModel]:
    """Immutable JSON settings; every construction receives an independently validated model."""

    model: type[S] = field(repr=False)
    _json: str = field(repr=False)
    _outputs: tuple[PreparedOutput, ...] = field(default=(), repr=False)

    def materialize(self) -> S:
        result = self.model.model_validate_json(self._json)
        if (
            canonical(json.loads(result.model_dump_json(round_trip=True, by_alias=True)))
            != self._json
        ):
            raise TemplateError("settings must round-trip through JSON without changing values")
        return result

    def with_defaults(self, data: dict[str, Any]) -> dict[str, Any]:
        """Fill omitted optional fields from accepted values, without evaluating defaults again."""
        if issubclass(self.model, RootModel):
            return data
        accepted = self.materialize()
        for name, info in self.model.model_fields.items():
            if info.is_required():
                continue
            present = any(
                candidate.search_dict_for_path(data) is not PydanticUndefined
                if isinstance(candidate, AliasPath)
                else candidate in data
                for candidate in _validation_inputs(self.model.model_config, name, info)
            )
            key = info.serialization_alias or info.alias or name
            if not present:
                data[key] = copy.deepcopy(getattr(accepted, name))
        return data


@dataclass(frozen=True)
class ServiceTemplate[S: BaseModel]:
    """A trusted synchronous constructor and the fixed interface its children expose."""

    name: str
    revision: str
    service_class: type[CliffracerService]
    settings_model: type[S]
    factory: Callable[[S, ServiceConfig], CliffracerService] = field(repr=False)
    startup_timeout: float = 30.0
    cleanup_timeout: float = 30.0
    max_rpc_concurrency: int = 32
    max_async_rpc_concurrency: int = 32

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.revision.strip():
            raise TemplateError("template name and revision must be nonempty")
        if not isinstance(self.service_class, type) or not issubclass(
            self.service_class, CliffracerService
        ):
            raise TemplateError("template service_class must extend CliffracerService")
        if not isinstance(self.settings_model, type) or not issubclass(
            self.settings_model, BaseModel
        ):
            raise TemplateError("template settings_model must extend BaseModel")
        for budget in (self.startup_timeout, self.cleanup_timeout):
            if not math.isfinite(budget) or budget <= 0:
                raise TemplateError("template lifecycle budgets must be finite and positive")
        for limit in (self.max_rpc_concurrency, self.max_async_rpc_concurrency):
            if type(limit) is not int or limit < 1:
                raise TemplateError("template dispatch limits must be positive integers")
        if (
            not callable(self.factory)
            or inspect.iscoroutinefunction(self.factory)
            or inspect.iscoroutinefunction(type(self.factory).__call__)
        ):
            raise TemplateError("template factory must be synchronous and callable")
        try:
            inspect.signature(self.factory).bind(object(), object())
        except (TypeError, ValueError) as exc:
            raise TemplateError(
                "template factory must accept settings and runtime configuration"
            ) from exc


@dataclass(frozen=True)
class RegisteredTemplate[S: BaseModel]:
    definition: ServiceTemplate[S]
    contract: RpcContract
    _settings_schema: str = field(repr=False)
    output_contract: OutputContract = field(default_factory=OutputContract)

    def _check_definition(self) -> None:
        _check_surface(self.definition.service_class)
        self.contract.verify(describe(self.definition.service_class))
        self.output_contract.verify(describe_outputs(self.definition.service_class))
        if canonical(self.definition.settings_model.model_json_schema()) != self._settings_schema:
            raise TemplateError(
                "registered settings schema changed; register a new template revision"
            )

    def normalize(
        self,
        settings: S | Mapping[str, Any],
        *,
        defaults: NormalizedSettings[S] | None = None,
    ) -> NormalizedSettings[S]:
        """Validate and detach business inputs from caller-owned mutable objects."""
        self._check_definition()
        data = _python_copy(settings) if isinstance(settings, BaseModel) else dict(settings)
        data = copy.deepcopy(data)
        if defaults is not None:
            if defaults.model is not self.definition.settings_model:
                raise TemplateError("defaults belong to another template model")
            data = defaults.with_defaults(data)
        try:
            model = self.definition.settings_model.model_validate(data)
        except ValidationError as exc:
            if not isinstance(settings, BaseModel):
                raise
            # The caller's model was valid; what fails is the copy its own dump made of it.
            raise TemplateError(_LOST) from exc
        encoded = canonical(json.loads(model.model_dump_json(round_trip=True, by_alias=True)))
        document = json.loads(encoded)
        try:
            probe = _Probe(type(model), document)
        except ValidationError as exc:
            # The probe reads the model's own dump first: one the model cannot read back is a lossy
            # serializer's, and `materialize` below reads the same document.
            raise TemplateError(_LOST) from exc
        _check_settings_inputs(model, document, probe=probe)
        normalized = NormalizedSettings(
            self.definition.settings_model, encoded, prepare_outputs(self.output_contract, model)
        )
        materialized = normalized.materialize()
        # The declared fields by value, as `_same` compares: a dataclass declared `eq=False`
        # compares by identity, and the copy read back is a new object that holds the same values.
        # The extras exactly, at every model level (`_extras_read_back`): an extra has no declared
        # type to be read back as, so a set or a dataclass in one comes back as a list or a dict.
        if not _same(materialized, model) or not _extras_read_back(materialized, model):
            raise TemplateError(_LOST)
        # A model is copied through its own dump, which a serializer has already written: what is
        # stored is compared with the caller's model too, by value, field by field of the settings
        # model, the class it is read back as (a subclass's own fields are not stored).
        declared = self.definition.settings_model.model_fields
        if isinstance(settings, self.definition.settings_model) and not all(
            _same(getattr(materialized, name), getattr(settings, name)) for name in declared
        ):
            raise TemplateError(_LOST)
        return normalized

    def bind_outputs(
        self, settings: NormalizedSettings[S], runtime: ServiceConfig
    ) -> OutputBindings:
        """Plan accepted subject families for inspection and explicit broker grants."""
        self._check_definition()
        if settings.model is not self.definition.settings_model:
            raise TemplateError("settings belong to another template model")
        self.output_contract.verify([output.definition for output in settings._outputs])
        return OutputBindings(
            self.output_contract,
            tuple(
                ResolvedOutput(output, runtime.namespace, runtime.subject_prefix)
                for output in settings._outputs
            ),
        )

    def construct(
        self,
        settings: NormalizedSettings[S],
        runtime: ServiceConfig,
        *,
        bindings: OutputBindings | None = None,
    ) -> CliffracerService:
        """Construct and claim one fresh object, without starting or stopping resources."""
        self._check_definition()
        if settings.model is not self.definition.settings_model:
            raise TemplateError("settings belong to another template model")
        runtime_data = _copy_runtime_config(runtime).model_dump(mode="python", round_trip=True)
        runtime_data.update(
            max_rpc_concurrency=self.definition.max_rpc_concurrency,
            max_async_rpc_concurrency=self.definition.max_async_rpc_concurrency,
        )
        expected = ServiceConfig.model_validate(runtime_data)
        resolved = self.bind_outputs(settings, expected)
        if bindings is None:
            bindings = resolved
        elif (
            bindings.contract != resolved.contract
            or bindings.outputs != resolved.outputs
            or (bindings.producer is not None and bindings.producer.service != expected.name)
        ):
            raise TemplateError(
                "output bindings differ from the accepted settings or assigned runtime"
            )
        result: object = self.definition.factory(
            settings.materialize(), _copy_runtime_config(expected)
        )
        if type(result) is not self.definition.service_class:
            if inspect.iscoroutine(result):
                result.close()
            raise TemplateError(
                "factory must return a fresh instance of the exact registered service class"
            )
        instance = result
        with _construction_lock:
            lifecycle = instance.container.lifecycle
            if (
                id(instance) in _constructed
                or lifecycle.is_running
                or lifecycle.is_starting
                or lifecycle.is_stopped
                or lifecycle.stop_requested
                or lifecycle.active_tasks
                or instance.nc is not None
                or instance.health_listener.port is not None
            ):
                raise TemplateError("factory returned an already used or owned service instance")
            configurations = (
                instance.config,
                instance.container.config,
                lifecycle.config,
                instance.container.connection.config,
            )
            if (
                any(config != expected for config in configurations)
                or instance.health_listener.host != expected.health_host
            ):
                raise TemplateError("factory changed host-assigned runtime configuration")
            for extension in instance.extensions:
                _check_extension(extension.name, extension)
            for name, _signature in self.contract.signatures:
                if name in vars(instance):
                    raise TemplateError(f"factory overrides registered RPC handler {name!r}")
            for name in output_declarations(type(instance)):
                if name in vars(instance):
                    raise TemplateError(f"factory overrides registered output {name!r}")
            if instance.output_bindings is not None:
                raise TemplateError("factory preconfigured accepted output bindings")
            self._check_definition()
            instance._output_bindings = bindings
            _constructed[id(instance)] = instance
        return instance


class TemplateCatalog:
    """A process-local catalog of immutable application template revisions."""

    def __init__(self) -> None:
        self._templates: dict[tuple[str, str], RegisteredTemplate[Any]] = {}

    def register[S: BaseModel](self, template: ServiceTemplate[S]) -> RegisteredTemplate[S]:
        _check_surface(template.service_class)
        try:
            settings_schema = canonical(template.settings_model.model_json_schema())
        except ValueError as exc:
            raise TemplateError(
                f"template {template.name!r} revision {template.revision!r}: the settings schema "
                f"holds a number that is not finite (inf, -inf or nan), which JSON cannot carry. "
                f"Use None for 'no limit' (`float | None = None`), or leave the bound out: {exc}"
            ) from exc
        registered = RegisteredTemplate(
            template,
            RpcContract.from_description(describe(template.service_class)),
            settings_schema,
            OutputContract(describe_outputs(template.service_class)),
        )
        key = (template.name, template.revision)
        existing = self._templates.get(key)
        if existing is not None:
            if existing != registered:
                raise ActivationConflict(
                    f"template {template.name!r} revision {template.revision!r} is already registered"
                )
            return cast(RegisteredTemplate[S], existing)
        self._templates[key] = registered
        return registered

    def resolve(self, name: str, revision: str) -> RegisteredTemplate[Any]:
        try:
            return self._templates[(name, revision)]
        except KeyError:
            raise TemplateError(f"unknown template {name!r} revision {revision!r}") from None


__all__ = ["NormalizedSettings", "RegisteredTemplate", "ServiceTemplate", "TemplateCatalog"]
