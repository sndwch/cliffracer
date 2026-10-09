"""A template refuses, copies and guards settings and services as its errors say.

A nested model written as text is refused by name, and a list written as text or text written as a
list, read back through a validator, is accepted; so is a value read back as another kind it
equals. A set whose own serializer fails is refused, a set beside another field's serializer is
copied, and a `Json` field of a settings instance is stored as its text. A template needs a name
and a revision, takes a dispatch limit of one, and bounds a child that names no limit; its repr
shows neither its factory nor its stored settings, and neither it nor its settings can be
reassigned. Defaults, settings and bindings of another model, contract or runtime are refused, the
bindings planned for a template without outputs are taken, and a factory that returns a coroutine
is refused and the coroutine closed. A handler marker on a private or static member is refused at
registration, and one added after registration is refused by `normalize`, `bind_outputs` and
`construct`, before the settings are read.
"""

import dataclasses
import inspect
from typing import Annotated, Any

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Json,
    PlainSerializer,
    RootModel,
    create_model,
    field_serializer,
    field_validator,
)

from cliffracer import CliffracerService, OutputError, ServiceConfig, rpc, timer
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import TemplateError
from tests.fixtures.shipment_outputs import BatchSettings, shipment_output_template
from tests.fixtures.shipment_templates import (
    Shipments,
    ShipmentSettings,
    shipment_template,
)

pytestmark = pytest.mark.unit

LOST = "settings must round-trip through JSON without changing values"
NO_KEY = "settings field {!r} must round-trip through an accepted serialized key"
SHIPMENT = {"warehouse": "north", "destinations": ["a"]}


def _registered(model):
    return TemplateCatalog().register(shipment_template(settings_model=model))


def _registered_with(**overrides):
    return TemplateCatalog().register(shipment_template(**overrides))


def _outcome(model, settings) -> str:
    try:
        return "accepted " + _registered(model).normalize(settings)._json
    except TemplateError as refused:
        return f"refused: {refused}"


def _runtime(**options) -> ServiceConfig:
    return ServiceConfig(**{"name": "shipments_a", "health_port": 0, **options})


# --- the settings check reads a value written in another form -------------------------------


class Point(BaseModel):
    x: int = 0


class PointAsText(BaseModel):
    """A nested model written as text: its fields have no keys to be read from."""

    point: Point = Field(default_factory=Point)

    @field_serializer("point")
    def _as_text(self, value: Point) -> str:
        return str(value.x)

    @field_validator("point", mode="before")
    @classmethod
    def _from_text(cls, value: Any) -> Any:
        return {"x": int(value)} if isinstance(value, str) else value


def test_a_nested_model_written_as_text_is_refused_by_name_not_by_a_crash():
    assert _outcome(PointAsText, {"point": {"x": 2}}) == "refused: " + NO_KEY.format("x")


class ListAsText(BaseModel):
    """A list written as one comma-joined string, and read back from one."""

    tags: list[str] = Field(default_factory=list)

    @field_serializer("tags")
    def _joined(self, value: list[str]) -> str:
        return ",".join(value)

    @field_validator("tags", mode="before")
    @classmethod
    def _split(cls, value: Any) -> Any:
        return value.split(",") if isinstance(value, str) else value


class TextAsAList(BaseModel):
    """A string written as a one-item list, and read back from one."""

    code: str = "ab"

    @field_serializer("code")
    def _wrapped(self, value: str) -> list[str]:
        return [value]

    @field_validator("code", mode="before")
    @classmethod
    def _unwrapped(cls, value: Any) -> Any:
        return value[0] if isinstance(value, list) else value


def test_a_list_written_as_text_and_text_written_as_a_list_are_accepted():
    assert _outcome(ListAsText, {"tags": ["a", "b"]}) == 'accepted {"tags":"a,b"}'
    assert _outcome(TextAsAList, {"code": "ab"}) == 'accepted {"code":["ab"]}'


# --- the Python copy of a settings model ---------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Box:
    width: int = 1


class SetUnderAnAnnotatedSerializer(BaseModel):
    boxes: Annotated[frozenset[Box], PlainSerializer(lambda value: set(value))] = Field(
        default_factory=lambda: frozenset({Box(1)})
    )


class SetAtTheRootUnderItsOwnSerializer(RootModel[frozenset[Box]]):
    root: frozenset[Box] = Field(default_factory=lambda: frozenset({Box(1)}))

    @field_serializer("root")
    def _as_a_set(self, value: frozenset[Box]) -> set[Box]:
        return set(value)


@pytest.mark.parametrize(
    "model",
    [SetUnderAnAnnotatedSerializer, SetAtTheRootUnderItsOwnSerializer],
    ids=["an-annotated-serializer", "a-root-serializer"],
)
def test_a_set_whose_own_serializer_fails_is_refused_not_copied_past_it(model):
    assert _outcome(model, model()) == f"refused: {LOST}"


class SetBesideASerializedField(BaseModel):
    """The serializer is another field's; the set has none of its own and is copied as it is."""

    boxes: frozenset[Box] = Field(default_factory=lambda: frozenset({Box(2)}))
    label: str = "a"

    @field_serializer("label")
    def _label(self, value: str) -> str:
        return value


def test_a_set_beside_another_fields_serializer_is_copied():
    settings = SetBesideASerializedField()

    assert _registered(SetBesideASerializedField).normalize(settings).materialize() == settings


class HoldsJson(BaseModel):
    payload: Json[dict[str, int]] = Field(default='{"a": 1}')


def test_a_settings_instance_holding_a_json_field_is_stored_as_json_text_and_read_back():
    normalized = _registered(HoldsJson).normalize(HoldsJson(payload='{"a": 1}'))

    assert normalized._json == '{"payload":"{\\"a\\":1}"}'
    assert normalized.materialize().payload == {"a": 1}


class EqualToItsForm:
    """A value equal to the form it is written as, so it reads back as that form, not as itself."""

    def __init__(self, form: Any) -> None:
        self.form = form

    def __eq__(self, other: object) -> bool:
        return other == self.form

    __hash__ = None  # type: ignore[assignment]


def _written_as_its_form(value: Any) -> Any:
    return value.form if isinstance(value, EqualToItsForm) else value


@pytest.mark.parametrize(
    ("annotation", "form"),
    [
        (Point | EqualToItsForm, Point(x=2)),
        (list[int] | EqualToItsForm, [1, 2]),
        (dict[str, int] | EqualToItsForm, {"a": 1}),
    ],
    ids=["read-back-as-a-model", "read-back-as-a-list", "read-back-as-a-mapping"],
)
def test_a_value_read_back_as_another_kind_that_equals_it_is_accepted(annotation, form):
    """Only a model, list or mapping read back against one of the same kind can hold a model whose
    extras differ: a value read back as a model, list or mapping it equals is accepted. Given in a
    mapping, the value is validated as it is, so the model compared still holds it, not its form."""
    model = create_model(
        "HoldsAnEqualForm",
        __config__=ConfigDict(arbitrary_types_allowed=True),
        value=(Annotated[annotation, PlainSerializer(_written_as_its_form)], ...),
    )

    accepted = _registered(model).normalize({"value": EqualToItsForm(form)})

    assert accepted.materialize().value == form


# --- templates, defaults and immutability --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "revision"),
    [("", "r"), (" ", "r"), ("n", ""), ("n", " ")],
    ids=["no-name", "blank-name", "no-revision", "blank-revision"],
)
def test_a_template_without_a_name_or_revision_is_refused(name, revision):
    with pytest.raises(TemplateError) as refused:
        shipment_template(name=name, revision=revision)

    assert str(refused.value) == "template name and revision must be nonempty"


def test_a_dispatch_limit_of_one_is_accepted():
    """FLOOR: 1 is the smallest limit a template takes."""
    template = shipment_template(max_rpc_concurrency=1, max_async_rpc_concurrency=1)

    assert (template.max_rpc_concurrency, template.max_async_rpc_concurrency) == (1, 1)


def test_a_template_that_leaves_the_dispatch_limits_out_bounds_its_children_at_32():
    """CEILING: a child of a template naming no limit runs at most 32 RPCs of each kind at once."""
    registered = _registered(ShipmentSettings)
    child = registered.construct(registered.normalize(SHIPMENT), _runtime())

    assert 1 <= child.config.max_rpc_concurrency <= 32
    assert 1 <= child.config.max_async_rpc_concurrency <= 32


def test_a_template_repr_shows_neither_its_factory_nor_its_stored_settings():
    template = shipment_template()
    registered = TemplateCatalog().register(template)
    normalized = registered.normalize(SHIPMENT)

    assert repr(normalized) == "NormalizedSettings()"
    assert repr(template) == (
        f"ServiceTemplate(name='shipments', revision='warehouse-a', service_class={Shipments!r}, "
        f"settings_model={ShipmentSettings!r}, startup_timeout={template.startup_timeout!r}, "
        f"cleanup_timeout={template.cleanup_timeout!r}, "
        f"max_rpc_concurrency={template.max_rpc_concurrency!r}, "
        f"max_async_rpc_concurrency={template.max_async_rpc_concurrency!r})"
    )
    assert repr(registered) == (
        f"RegisteredTemplate(definition={template!r}, contract={registered.contract!r}, "
        f"output_contract={registered.output_contract!r})"
    )


def test_registered_templates_and_normalized_settings_cannot_be_reassigned():
    registered = _registered(ShipmentSettings)
    normalized = registered.normalize(SHIPMENT)

    with pytest.raises(dataclasses.FrozenInstanceError):
        normalized._json = "{}"
    with pytest.raises(dataclasses.FrozenInstanceError):
        registered._settings_schema = "{}"


class OtherSettings(BaseModel):
    warehouse: str = "south"
    destinations: list[str] = Field(default_factory=lambda: ["z"])


def test_defaults_of_another_template_model_are_refused():
    other = _registered(OtherSettings).normalize({})

    with pytest.raises(TemplateError) as refused:
        _registered(ShipmentSettings).normalize({"warehouse": "north"}, defaults=other)

    assert str(refused.value) == "defaults belong to another template model"


def test_bind_outputs_refuses_settings_of_another_template_model():
    other = _registered(OtherSettings).normalize({})

    with pytest.raises(TemplateError) as refused:
        _registered(ShipmentSettings).bind_outputs(other, _runtime())

    assert str(refused.value) == "settings belong to another template model"


class Quiet(CliffracerService):
    """A service for the same settings model as the output template, declaring no outputs."""

    def __init__(self, settings: BatchSettings, runtime: ServiceConfig):
        super().__init__(runtime)

    @rpc
    async def ping(self) -> int:
        return 1


def test_bind_outputs_refuses_settings_prepared_for_another_output_contract():
    with_outputs = TemplateCatalog().register(shipment_output_template())
    accepted = with_outputs.normalize({"warehouse": "north", "batch": "batch_a"})
    quiet = TemplateCatalog().register(
        shipment_output_template(name="quiet", service_class=Quiet, factory=Quiet)
    )

    with pytest.raises(OutputError):
        quiet.bind_outputs(accepted, _runtime(name="quiet_a"))


def test_construct_refuses_bindings_planned_for_another_runtime():
    template = TemplateCatalog().register(shipment_output_template())
    accepted = template.normalize({"warehouse": "north", "batch": "batch_a"})
    elsewhere = template.bind_outputs(accepted, _runtime(name="shipment_batch_a", namespace="west"))

    with pytest.raises(TemplateError) as refused:
        template.construct(
            accepted, _runtime(name="shipment_batch_a", namespace="east"), bindings=elsewhere
        )

    assert str(refused.value) == (
        "output bindings differ from the accepted settings or assigned runtime"
    )


def test_construct_takes_the_bindings_planned_for_a_template_without_outputs():
    """Bindings with no producer are compared by contract and outputs alone."""
    registered = _registered(ShipmentSettings)
    accepted = registered.normalize(SHIPMENT)
    planned = registered.bind_outputs(accepted, _runtime())
    assert planned.producer is None

    child = registered.construct(accepted, _runtime(), bindings=planned)

    assert type(child) is Shipments


def test_a_factory_returning_a_coroutine_is_refused_and_the_coroutine_closed():
    made: list[Any] = []

    async def build() -> None:
        return None

    def factory(settings: Any, runtime: Any) -> Any:
        made.append(build())
        return made[0]

    registered = _registered_with(factory=factory)

    with pytest.raises(TemplateError) as refused:
        registered.construct(registered.normalize(SHIPMENT), _runtime())

    assert str(refused.value) == (
        "factory must return a fresh instance of the exact registered service class"
    )
    assert inspect.getcoroutinestate(made[0]) == inspect.CORO_CLOSED


# --- the service surface, at registration and after it ------------------------------------------


def _fresh_class() -> type[Shipments]:
    return type("FreshShipments", (Shipments,), {})


async def _tick(self) -> None:
    return None


def test_a_private_member_carrying_a_handler_marker_is_refused_with_the_rename_advice():
    """The template surface check skips private names: discovery's own refusal, which says to
    rename the member, is the one reported."""
    fresh = _fresh_class()
    fresh._tick = timer(interval=60)(_tick)

    with pytest.raises(ConfigurationError) as refused:
        TemplateCatalog().register(shipment_template(service_class=fresh, factory=fresh))

    assert type(refused.value) is ConfigurationError
    assert str(refused.value).startswith(
        "FreshShipments._tick is decorated with @timer, but its name starts with an underscore"
    )


def test_a_static_method_carrying_a_timer_marker_is_refused():
    fresh = _fresh_class()
    fresh.tick = staticmethod(timer(interval=60)(_tick))

    with pytest.raises(TemplateError) as refused:
        TemplateCatalog().register(shipment_template(service_class=fresh, factory=fresh))

    assert str(refused.value) == "unsupported template declaration tick: _cliffracer_timers"


def _after_registration_a_timer_is_added():
    fresh = _fresh_class()
    registered = TemplateCatalog().register(shipment_template(service_class=fresh, factory=fresh))
    normalized = registered.normalize(SHIPMENT)
    fresh.tick = timer(interval=60)(_tick)
    return registered, normalized


TIMER_ADDED = "unsupported template declaration tick: _cliffracer_timers"


def test_normalize_refuses_a_service_class_given_a_timer_after_registration():
    registered, _ = _after_registration_a_timer_is_added()

    with pytest.raises(TemplateError) as refused:
        registered.normalize(SHIPMENT)

    assert str(refused.value) == TIMER_ADDED


def test_bind_outputs_refuses_a_service_class_given_a_timer_after_registration():
    registered, normalized = _after_registration_a_timer_is_added()

    with pytest.raises(TemplateError) as refused:
        registered.bind_outputs(normalized, _runtime())

    assert str(refused.value) == TIMER_ADDED


def test_construct_reports_a_changed_service_class_before_it_reads_the_settings():
    registered, _ = _after_registration_a_timer_is_added()
    other = _registered(OtherSettings).normalize({})

    with pytest.raises(TemplateError) as refused:
        registered.construct(other, _runtime())

    assert str(refused.value) == TIMER_ADDED
