"""A service whose parameters have defaults that are models, lists of models and dicts of models."""

import datetime
import decimal
import enum
import uuid
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Json,
    PlainSerializer,
    SecretStr,
    Strict,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from cliffracer import CliffracerService, rpc


class Item(BaseModel):
    name: str
    color: str = "red"
    tags: list[str] = Field(default_factory=list)


class Box(BaseModel):
    """A model with a model in it."""

    label: str
    item: Item


class Stock(CliffracerService):
    @rpc
    async def one(self, item: Item = Item(name="x", tags=["a"])) -> str:
        return item.name

    @rpc
    async def maybe(self, item: Item | None = None, other: Item | None = Item(name="y")) -> str:
        return (item or other or Item(name="")).name

    @rpc
    async def many(
        self,
        items: list[Item] = [Item(name="a"), Item(name="b")],  # noqa: B006
    ) -> int:
        return len(items)

    @rpc
    async def by_name(
        self,
        items: dict[str, Item] = {"first": Item(name="a")},  # noqa: B006
    ) -> int:
        return len(items)

    @rpc
    async def boxed(self, box: Box = Box(label="l", item=Item(name="inner"))) -> str:
        return box.item.name


class AVeryLongNamedModelThatTheCatalogueUsesForItsEntriesInTheStore(BaseModel):
    """A model whose name alone nearly fills a line."""

    name: str


class Bounded(BaseModel):
    """A model whose value has a constraint the schema's keys do not show."""

    count: int = Field(gt=0)


class Aliased(BaseModel):
    """An alias, and the field name accepted too."""

    model_config = ConfigDict(populate_by_name=True)

    item_name: str = Field(alias="itemName")


class StrictAliased(BaseModel):
    """An alias and nothing else: the field name is not accepted."""

    item_name: str = Field(alias="itemName")


class Wrapper(BaseModel):
    """Aliased models inside a model."""

    label: str
    strict: StrictAliased
    populatable: Aliased


class Aliasing(CliffracerService):
    @rpc
    async def populatable(self, item: Aliased = Aliased(itemName="p")) -> str:
        return item.item_name

    @rpc
    async def strict(self, item: StrictAliased = StrictAliased(itemName="s")) -> str:
        return item.item_name

    @rpc
    async def nested(
        self,
        wrapper: Wrapper = Wrapper(
            label="l", strict=StrictAliased(itemName="s"), populatable=Aliased(itemName="p")
        ),
    ) -> str:
        return wrapper.label

    @rpc
    async def many(
        self,
        items: list[StrictAliased] = [StrictAliased(itemName="a")],  # noqa: B006
    ) -> int:
        return len(items)

    @rpc
    async def by_name(
        self,
        items: dict[str, Aliased] = {"k": Aliased(itemName="a")},  # noqa: B006
    ) -> int:
        return len(items)

    @rpc
    async def plain(self, item: Item = Item(name="x")) -> str:
        return item.name


class Shade(enum.Enum):
    red = "red"
    blue = "blue"


class StrictModel(BaseModel):
    """A model declared strict, with the types whose JSON form is not their Python form."""

    model_config = ConfigDict(strict=True)

    when: datetime.datetime
    ident: uuid.UUID
    shade: Shade
    tags: set[str]
    raw: bytes
    pair: tuple[int, int]


class StrictDecimal(BaseModel):
    """Strict, with a Decimal: the schema describes it as `number | string`, a union."""

    model_config = ConfigDict(strict=True)

    amount: decimal.Decimal


class StrictField(BaseModel):
    """Strict by `Field`, in a model that is not."""

    when: datetime.datetime = Field(strict=True)


class StrictAnnotated(BaseModel):
    """Strict by `Strict()`, in a model that is not."""

    when: Annotated[datetime.datetime, Strict()]


_WHEN = datetime.datetime(2026, 1, 2, 3, 4, 5)
_STRICT = StrictModel(
    when=_WHEN,
    ident=uuid.UUID(int=1),
    shade=Shade.blue,
    tags={"t"},
    raw=b"x",
    pair=(3, 4),
)


class Strictness(CliffracerService):
    @rpc
    async def model_level(self, value: StrictModel = _STRICT) -> str:
        return value.shade.value

    @rpc
    async def money(
        self, value: StrictDecimal = StrictDecimal(amount=decimal.Decimal("1.5"))
    ) -> str:
        return str(value.amount)

    @rpc
    async def field_level(self, value: StrictField = StrictField(when=_WHEN)) -> str:
        return str(value.when)

    @rpc
    async def annotated(self, value: StrictAnnotated = StrictAnnotated(when=_WHEN)) -> str:
        return str(value.when)

    @rpc
    async def many(
        self,
        values: list[StrictField] = [StrictField(when=_WHEN)],  # noqa: B006
    ) -> int:
        return len(values)

    @rpc
    async def by_name(
        self,
        values: dict[str, StrictAnnotated] = {"k": StrictAnnotated(when=_WHEN)},  # noqa: B006
    ) -> int:
        return len(values)


class DateOrDatetime(BaseModel):
    """A union whose JSON form does not say which member it was: midnight is also a date."""

    when: datetime.date | datetime.datetime


class FloatOrDecimal(BaseModel):
    """A union whose JSON form does not say which member it was: 0.1 is also a float."""

    amount: float | decimal.Decimal


class IntOrStr(BaseModel):
    value: int | str


class Unioned(BaseModel):
    """A union two levels down, inside a list and a dict."""

    label: str
    rows: list[list[int | str]]
    by_key: dict[str, int | str]


class Plain(BaseModel):
    """No union anywhere, an optional model included."""

    label: str
    inner: Item | None = None
    tags: list[str] = Field(default_factory=list)


class Unions(CliffracerService):
    @rpc
    async def midnight(
        self, value: DateOrDatetime = DateOrDatetime(when=datetime.datetime(2026, 1, 1))
    ) -> str:
        return str(value.when)

    @rpc
    async def tenth(
        self, value: FloatOrDecimal = FloatOrDecimal(amount=decimal.Decimal("0.1"))
    ) -> str:
        return str(value.amount)

    @rpc
    async def either(self, value: IntOrStr = IntOrStr(value="1")) -> str:
        return str(value.value)

    @rpc
    async def deep(
        self,
        value: Unioned = Unioned(label="l", rows=[[1, "a"]], by_key={"k": 2}),
    ) -> str:
        return value.label

    @rpc
    async def listed(
        self,
        values: list[IntOrStr] = [IntOrStr(value=1)],  # noqa: B006
    ) -> int:
        return len(values)

    @rpc
    async def plain(
        self,
        value: Plain = Plain(label="p", inner=Item(name="i"), tags=["t"]),
    ) -> str:
        return value.label


class Swapped(BaseModel):
    """Two fields whose aliases are each other's names: the dump's keys validate, and swap."""

    a: int = Field(alias="b")
    b: int = Field(alias="a")


class Extra(BaseModel):
    """Keeps whatever else it is given."""

    model_config = ConfigDict(extra="allow")

    x: int


class Cat(BaseModel):
    kind: Literal["cat"] = "cat"
    lives: int = 9


class Dog(BaseModel):
    kind: Literal["dog"] = "dog"
    good: bool = True


class Owner(BaseModel):
    pet: Cat | Dog


class Household(BaseModel):
    """A union of models two levels down."""

    name: str
    owner: Owner


class Shapes(CliffracerService):
    @rpc
    async def swapped(self, value: Swapped = Swapped(b=1, a=2)) -> int:
        return value.a

    @rpc
    async def extra(self, value: Extra = Extra(x=1, stray="s")) -> int:
        return value.x

    @rpc
    async def bare_extra(self, value: Extra = Extra(x=2)) -> int:
        return value.x

    @rpc
    async def household(
        self, value: Household = Household(name="h", owner=Owner(pet=Cat(lives=3)))
    ) -> str:
        return value.name


class Changed(BaseModel):
    """A field serializer that changes the field's type: the dump is not what the model validates."""

    n: int

    @field_serializer("n")
    def _as_text(self, value: int) -> str:
        return f"#{value}"


class Doubled(BaseModel):
    """A serializer that is not idempotent: validating its output and dumping doubles it again."""

    n: Annotated[int, PlainSerializer(lambda value: value * 2, return_type=int)]


class Whole(BaseModel):
    """A model serializer that writes a different shape."""

    a: int

    @model_serializer
    def _as_total(self) -> dict[str, Any]:
        return {"total": self.a}


class Jsoned(BaseModel):
    """A field validated from JSON text and dumped as the parsed value."""

    data: Json[dict[str, int]]


class Base64Out(BaseModel):
    """Bytes dumped as base64 and validated as text: the double encoding."""

    model_config = ConfigDict(ser_json_bytes="base64")

    raw: bytes


class Base64Both(BaseModel):
    """Bytes dumped and validated as base64."""

    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    raw: bytes


class Secret(BaseModel):
    """A secret dumps masked."""

    token: SecretStr


class Appends(BaseModel):
    """A validator that is not idempotent."""

    text: str

    @field_validator("text")
    @classmethod
    def _bang(cls, value: str) -> str:
        return value + "!"


class AddsOne(BaseModel):
    """A before-validator that is not idempotent."""

    n: int

    @model_validator(mode="before")
    @classmethod
    def _plus_one(cls, data: Any) -> Any:
        return {**data, "n": int(data["n"]) + 1}


class Awkward(CliffracerService):
    @rpc
    async def changed(self, value: Changed = Changed(n=1)) -> int:
        return value.n

    @rpc
    async def doubled(self, value: Doubled = Doubled(n=2)) -> int:
        return value.n

    @rpc
    async def whole(self, value: Whole = Whole(a=1)) -> int:
        return value.a

    @rpc
    async def jsoned(self, value: Jsoned = Jsoned(data='{"a": 1}')) -> int:
        return len(value.data)

    @rpc
    async def base64_out(self, value: Base64Out = Base64Out(raw=b"hello")) -> int:
        return len(value.raw)

    @rpc
    async def base64_both(self, value: Base64Both = Base64Both(raw=b"hello")) -> int:
        return len(value.raw)

    @rpc
    async def secret(self, value: Secret = Secret(token=SecretStr("hunter2"))) -> int:
        return len(value.token.get_secret_value())

    @rpc
    async def appends(self, value: Appends = Appends.model_construct(text="a!")) -> str:
        return value.text

    @rpc
    async def adds_one(self, value: AddsOne = AddsOne.model_construct(n=2)) -> int:
        return value.n


class Live(Shapes, Awkward, Strictness):
    """Every awkward default in one service, for a live describe over a broker."""
