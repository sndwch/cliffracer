"""Typed outbound declarations, accepted routing values and generation metadata."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, overload

from pydantic import BaseModel

from .discovery import HandlerDiscovery
from .exceptions import CliffracerError
from .subjects import validate_subject

if TYPE_CHECKING:
    from .service import CliffracerService


class OutputError(CliffracerError):
    """An outbound declaration, binding or publication violates its contract."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _names(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise OutputError("output parameter names must be a collection of simple identifiers")
    values = tuple(values)
    if any(
        not isinstance(value, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value) is None
        for value in values
    ):
        raise OutputError("output parameter names must be a collection of simple identifiers")
    if len(set(values)) != len(values):
        raise OutputError("output parameter names must be unique")
    return tuple(sorted(values))


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in ".{}"):
        raise OutputError(f"output parameter {name!r} must be a single concrete subject token")
    try:
        validate_subject(value, wildcards=False)
    except ValueError:
        raise OutputError(
            f"output parameter {name!r} must be a single concrete subject token"
        ) from None
    return value


def _tokens(expression: str) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(expression, str) or not expression:
        raise OutputError("an output subject expression must be nonempty")
    result = []
    for part in expression.split("."):
        match = re.fullmatch(r"\{([A-Za-z][A-Za-z0-9_]*)\}", part)
        if match:
            result.append((part, match[1]))
        else:
            result.append((_token(part, "literal"), None))
    return tuple(result)


@dataclass(frozen=True)
class OutputDescription:
    """A serializable contract containing declarations and schemas, without instance settings."""

    name: str
    subject: str
    settings: tuple[str, ...]
    parameters: tuple[str, ...]
    _schema: str = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "subject": self.subject,
            "settings": list(self.settings),
            "parameters": list(self.parameters),
            "schema": json.loads(self._schema),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OutputDescription:
        return cls(
            value["name"],
            value["subject"],
            tuple(value["settings"]),
            tuple(value["parameters"]),
            _canonical(value["schema"]),
        )


@dataclass(frozen=True)
class OutputContract:
    """Named output schemas and routing declarations, independent of the RPC method table."""

    outputs: tuple[OutputDescription, ...] = ()

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.outputs, key=lambda output: output.name))
        if len({output.name for output in ordered}) != len(ordered):
            raise OutputError("output names must be unique")
        object.__setattr__(self, "outputs", ordered)

    @property
    def identity(self) -> str:
        encoded = _canonical([output.to_dict() for output in self.outputs])
        return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()

    def verify(self, outputs: tuple[OutputDescription, ...] | list[OutputDescription]) -> None:
        actual = {output.name: output for output in outputs}
        if len(actual) != len(outputs):
            raise OutputError("outbound description repeats output names")
        expected = {output.name: output for output in self.outputs}
        if actual != expected:
            changed = sorted(
                name
                for name in actual.keys() | expected.keys()
                if actual.get(name) != expected.get(name)
            )
            raise OutputError(f"outbound contract changed for outputs: {changed}")


@dataclass(frozen=True)
class Output[T: BaseModel]:
    """Declare a typed output; placeholders occupy whole subject tokens."""

    model: type[T]
    subject: str
    settings: tuple[str, ...] = ()
    parameters: tuple[str, ...] = ()
    _name: str = field(default="", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.model, type) or not issubclass(self.model, BaseModel):
            raise OutputError("an output payload must be a Pydantic model")
        object.__setattr__(self, "settings", _names(self.settings))
        object.__setattr__(self, "parameters", _names(self.parameters))
        if set(self.settings) & set(self.parameters):
            raise OutputError("activation settings and publication parameters must be distinct")
        mentioned = {parameter for _, parameter in _tokens(self.subject) if parameter is not None}
        if mentioned != set(self.settings) | set(self.parameters):
            raise OutputError(
                "declare every subject placeholder exactly once as a setting or parameter"
            )

    def __set_name__(self, owner: type, name: str) -> None:
        _names((name,))
        if self._name and self._name != name:
            raise OutputError("one output declaration cannot have multiple names")
        object.__setattr__(self, "_name", name)

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Output[T]: ...

    @overload
    def __get__(self, instance: CliffracerService, owner: type | None = None) -> BoundOutput[T]: ...

    def __get__(
        self, instance: CliffracerService | None, owner: type | None = None
    ) -> Output[T] | BoundOutput[T]:
        if instance is None:
            return self
        return BoundOutput(instance, self)

    def describe(self, name: str) -> OutputDescription:
        return OutputDescription(
            name,
            self.subject,
            self.settings,
            self.parameters,
            _canonical(
                {
                    "validation": self.model.model_json_schema(mode="validation"),
                    "serialization": self.model.model_json_schema(mode="serialization"),
                }
            ),
        )

    def encode(self, value: T) -> dict[str, Any]:
        """Validate the actual serialized payload, including constructed or mutated instances."""
        if type(value) is not self.model:
            raise OutputError(f"output {self._name!r} requires its declared payload model")
        encoded = value.model_dump_json(round_trip=True, by_alias=True, warnings="error")
        checked = self.model.model_validate_json(encoded, strict=True)
        data = json.loads(encoded)
        if not isinstance(data, dict) or _canonical(data) != _canonical(
            json.loads(checked.model_dump_json(round_trip=True, by_alias=True, warnings="error"))
        ):
            raise OutputError(f"output {self._name!r} payload must round-trip as a JSON object")
        return data


def output_declarations(service_class: type) -> dict[str, Output[Any]]:
    declarations = {
        name: value
        for name, value in inspect.getmembers_static(service_class)
        if isinstance(value, Output)
    }
    if any(value._name != name for name, value in declarations.items()):
        raise OutputError("outputs must be declared under their bound class attribute names")
    return declarations


def describe_outputs(service_class: type) -> tuple[OutputDescription, ...]:
    return tuple(value.describe(name) for name, value in output_declarations(service_class).items())


@dataclass(frozen=True)
class PreparedOutput:
    """Accepted literal tokens and the explicitly declared publication-time positions."""

    definition: OutputDescription
    parts: tuple[str, ...]
    parameters: tuple[tuple[int, str], ...]


def prepare_outputs(contract: OutputContract, settings: BaseModel) -> tuple[PreparedOutput, ...]:
    prepared = []
    for output in contract.outputs:
        if not set(output.settings) <= type(settings).model_fields.keys():
            raise OutputError(f"output {output.name!r} references an undeclared settings field")
        parts: list[str] = []
        parameters: list[tuple[int, str]] = []
        for literal, parameter in _tokens(output.subject):
            if parameter in output.settings:
                literal = _token(getattr(settings, parameter), parameter)
            elif parameter is not None:
                parameters.append((len(parts), parameter))
            parts.append(literal)
        prepared.append(PreparedOutput(output, tuple(parts), tuple(parameters)))
    return tuple(prepared)


@dataclass(frozen=True)
class ResolvedOutput:
    """A scoped subject family whose activation values have already been accepted."""

    prepared: PreparedOutput
    namespace: str | None
    subject_prefix: str | None

    def _scoped(self, parts: list[str]) -> str:
        return HandlerDiscovery.scoped_subject(
            ".".join(parts), namespace=self.namespace, subject_prefix=self.subject_prefix
        )

    @property
    def subject(self) -> str:
        return self._scoped(list(self.prepared.parts))

    @property
    def publish_subject(self) -> str:
        parts = list(self.prepared.parts)
        for index, _ in self.prepared.parameters:
            parts[index] = "*"
        return validate_subject(self._scoped(parts))

    def resolve(self, parameters: Mapping[str, str]) -> str:
        expected = set(self.prepared.definition.parameters)
        if set(parameters) != expected:
            raise OutputError(
                f"output {self.prepared.definition.name!r} expects publication parameters {sorted(expected)}"
            )
        parts = list(self.prepared.parts)
        for index, name in self.prepared.parameters:
            parts[index] = _token(parameters[name], name)
        return validate_subject(self._scoped(parts), wildcards=False)


@dataclass(frozen=True)
class OutputProducer:
    """Public activation identity carried by output events; it grants no authority."""

    scope: str
    template: str
    key: str
    revision: str
    incarnation: str
    generation: int
    service: str


@dataclass(frozen=True)
class OutputBindings:
    """Inspectable accepted routes, their contract and optional activation identity."""

    contract: OutputContract = field(default_factory=OutputContract)
    outputs: tuple[ResolvedOutput, ...] = ()
    producer: OutputProducer | None = None

    @property
    def publish_subjects(self) -> tuple[str, ...]:
        return tuple(sorted({output.publish_subject for output in self.outputs}))

    def output(self, name: str) -> ResolvedOutput:
        for output in self.outputs:
            if output.prepared.definition.name == name:
                return output
        raise OutputError(f"output {name!r} is not bound")

    def metadata(self, name: str) -> dict[str, Any]:
        self.output(name)
        return {
            "name": name,
            "contract": self.contract.identity,
            "producer": asdict(self.producer) if self.producer is not None else None,
        }

    def accepts(self, metadata: Mapping[str, Any]) -> bool:
        """Recognize this producer generation and contract; this is not authentication or fencing."""
        if self.producer is None:
            return False
        try:
            return _canonical(dict(metadata)) == _canonical(self.metadata(metadata["name"]))
        except (KeyError, TypeError, ValueError, OutputError):
            return False


@dataclass(frozen=True)
class BoundOutput[T: BaseModel]:
    _service: CliffracerService = field(repr=False)
    _declaration: Output[T] = field(repr=False)

    async def publish(
        self,
        value: T,
        *,
        parameters: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        bindings = self._service.output_bindings
        if bindings is None:
            raise OutputError("typed outputs must be bound before publication")
        bindings.contract.verify(describe_outputs(type(self._service)))
        name = self._declaration._name
        subject = bindings.output(name).resolve(parameters or {})
        data = self._declaration.encode(value)
        return await self._service._publish_bound_output(
            subject, data, bindings.metadata(name), idempotency_key=idempotency_key
        )


__all__ = [
    "Output",
    "OutputError",
    "OutputContract",
    "OutputDescription",
    "OutputBindings",
    "OutputProducer",
]
