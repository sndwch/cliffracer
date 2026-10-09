"""Values a KV write is given: nested containers, models and dataclasses, some holding a secret.

`generate(rng)` builds one value and says how many secrets it planted. A secret is a `SecretStr`, a
`SecretBytes` or a `Secret[int]`, planted as a value at any depth: a dict value or key (of a dict, an
`OrderedDict` or another dict subclass), a list, tuple, set or frozenset item, a model field, a
model's extra, what a computed field returns, a stdlib or Pydantic dataclass field. Dict keys are strings or ints, which a refusal spells differently. A field declared
`exclude=True`, of a model or a Pydantic dataclass, may hold a secret too: it is not stored, so it is
not counted as planted, and the value must be stored. About half the values plant none, so the clean
side is exercised as much as the planted one.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import random
import uuid
from collections import OrderedDict
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    Secret,
    SecretBytes,
    SecretStr,
    computed_field,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass

#: How deep the containers nest.
DEPTH = 3


class Shade(enum.Enum):
    RED = "red"


@dataclasses.dataclass
class Pair:
    a: Any = None
    b: Any = None


@pydantic_dataclass
class CheckedPair:
    a: Any = None
    b: Any = None


class Holder(BaseModel):
    model_config = ConfigDict(extra="allow")

    a: Any = None
    b: Any = None


class Ledger(dict):
    """A dict subclass: walked as a dict, though it is not exactly one."""


class Shown(BaseModel):
    """Holds a value only a computed field returns."""

    a: Any = None
    _held: Any = PrivateAttr(None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def held(self) -> Any:
        return self._held


class Hides(BaseModel):
    a: Any = None
    hidden: Any = Field(None, exclude=True)


@pydantic_dataclass
class CheckedHides:
    a: Any = None
    hidden: Any = Field(None, exclude=True)


class _Planter:
    def __init__(self, rng: random.Random, plant: bool) -> None:
        self.rng = rng
        self.plant = plant
        self.planted = 0

    def secret(self) -> Any:
        self.planted += 1
        return self.rng.choice(
            [lambda: SecretStr("pw"), lambda: SecretBytes(b"pw"), lambda: Secret[int](7)]
        )()

    def leaf(self, plant: bool = True) -> Any:
        if plant and self.plant and self.rng.random() < 0.15:
            return self.secret()
        return self.rng.choice(
            [
                lambda: self.rng.randint(-9, 9),
                lambda: self.rng.random(),
                lambda: "s",
                lambda: None,
                lambda: True,
                lambda: datetime.datetime(2020, 1, 1),
                lambda: uuid.UUID(int=self.rng.getrandbits(64)),
                lambda: decimal.Decimal("1.5"),
                lambda: Shade.RED,
            ]
        )()

    def key(self, index: int) -> Any:
        if self.plant and self.rng.random() < 0.05:
            return self.secret()
        return f"k{index}" if self.rng.random() < 0.7 else index

    def hidden(self) -> Any:
        """A value for a field that is not stored: a secret there is not counted as planted."""
        if self.rng.random() < 0.5:
            return self.rng.choice([SecretStr("pw"), SecretBytes(b"pw"), Secret[int](7)])
        return self.leaf(plant=False)

    def value(self, depth: int = 0) -> Any:
        if depth > DEPTH or self.rng.random() < 0.3:
            return self.leaf()
        kind = self.rng.randint(0, 12)
        if kind == 0:
            return [self.value(depth + 1) for _ in range(self.rng.randint(0, 3))]
        if kind == 1:
            return {self.key(i): self.value(depth + 1) for i in range(self.rng.randint(0, 3))}
        if kind == 2:
            return tuple(self.value(depth + 1) for _ in range(self.rng.randint(0, 3)))
        if kind == 3:
            return Pair(self.value(depth + 1), self.value(depth + 1))
        if kind == 4:
            return CheckedPair(self.value(depth + 1), self.value(depth + 1))
        if kind == 5:
            return Holder(
                a=self.value(depth + 1), b=self.value(depth + 1), extra=self.value(depth + 1)
            )
        if kind == 6:
            shown = Shown(a=self.value(depth + 1))
            shown._held = self.value(depth + 1)
            return shown
        if kind == 7:
            return Hides(a=self.value(depth + 1), hidden=self.hidden())
        if kind == 8:
            return CheckedHides(a=self.value(depth + 1), hidden=self.hidden())
        if kind == 9:  # a set holds only what hashes: leaves
            return {self.leaf() for _ in range(self.rng.randint(0, 3))}
        if kind == 10:
            return frozenset(self.leaf() for _ in range(self.rng.randint(0, 3)))
        if kind == 11:
            subclass = self.rng.choice([OrderedDict, Ledger])
            return subclass(
                (self.key(i), self.value(depth + 1)) for i in range(self.rng.randint(0, 3))
            )
        return self.leaf()


def generate(rng: random.Random) -> tuple[Any, int]:
    """One value and the number of secrets planted in it."""
    planter = _Planter(rng, plant=rng.random() < 0.5)
    value = planter.value()
    return value, planter.planted
