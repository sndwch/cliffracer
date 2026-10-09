"""Fixed model shapes where a send path once chose a form the declared class misread, each with the
outcome it has on each path: `wire` (`wire_models`) and `client` (`ServiceClient._encode`).

Every outcome is EQUAL or REFUSED except one, on the wire, where the handler declares the subclass
whose validation alias the dumps do not carry: H-W covers it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

BY_NAME = ConfigDict(validate_by_alias=False, validate_by_name=True)
T = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class ACcollide(BaseModel):
    a: int = Field(0, validation_alias=AliasChoices("b", "a"))
    b: int = 0


class APindex(BaseModel):
    x: int = Field(0, validation_alias=AliasPath("p", 0))


class APindex1(BaseModel):
    x: int = Field(0, validation_alias=AliasPath("p", 1))


class APshared(BaseModel):
    x: int = Field(0, validation_alias=AliasPath("p", "a"))
    y: int = Field(0, validation_alias=AliasPath("p", "b"))


class APplusAlias(BaseModel):
    x: int = Field(0, validation_alias=AliasPath("p", "a"))
    y: int = Field(0, alias="Y")


class Populatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    x: int = Field(0, validation_alias=AliasChoices("xx"))


class StrictAC(BaseModel):
    model_config = ConfigDict(strict=True)
    when: datetime = Field(T, validation_alias=AliasChoices("W"))


class InnerAP(BaseModel):
    x: int = Field(0, validation_alias=AliasPath("p", "a"))


class OuterByName(BaseModel):
    model_config = BY_NAME
    name: str = Field("n", alias="Name")
    inner: InnerAP


class SerAC(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices("xx"))

    @field_serializer("x")
    def _s(self, v: int) -> int:
        return v


class Base9(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices("y", "x"))


class Sub9(Base9):
    x: int = Field(0, validation_alias="z")


class SerByAliasNameRead(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    x: int = Field(0, serialization_alias="X")


class SerByAliasNameReq(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    x: int = Field(serialization_alias="X")


class PrefixCollide(BaseModel):
    p: dict = {}
    x: int = Field(0, validation_alias=AliasPath("p", "a"))


class ACpathFirst(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices(AliasPath("p", "a"), "x"))


class BumpAC(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices("xx"))

    @field_validator("x")
    @classmethod
    def _b(cls, v: int) -> int:
        return v + 1


class APrequired(BaseModel):
    x: int = Field(validation_alias=AliasPath("p", "a"))


class ACsecond(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices("q", "x"))


class AppModel(BaseModel):
    """A project base: configuration, no fields."""

    model_config = ConfigDict(str_strip_whitespace=True)


class ACoverAppModel(AppModel):
    x: int = Field(0, validation_alias=AliasChoices("xx"))


class Defaults(BaseModel):
    note: str = "n"


class ACoverDefaults(Defaults):
    x: int = Field(0, validation_alias=AliasChoices("xx"))


class Sub9SerAlias(Base9):
    model_config = ConfigDict(serialize_by_alias=True)
    x: int = Field(0, validation_alias="z", serialization_alias="X")


class TwoFieldsOneKey(BaseModel):
    a: int = Field(validation_alias="b")
    b: int = Field(validation_alias="b")
    p: dict = Field(validation_alias=AliasChoices("r", "b"))


class ABase(BaseModel):
    x: int = 0


class ASub(ABase):
    x: int = Field(0, validation_alias=AliasChoices("xx"))


class MBBase(BaseModel):
    a: int = 0
    p: dict = Field({}, validation_alias=AliasChoices("q", "b"))
    b: int = 0


class MBSub(MBBase):
    a: int = Field(0, validation_alias=AliasPath("q", "k"))
    p: dict = Field({})
    b: int = Field(0, alias="a")


class PSboth(BaseModel):
    x: int = Field(0, alias="X")
    y: int = Field(0, validation_alias=AliasChoices("yy"))


def _by_name(cls: type[BaseModel], data: dict[str, Any]) -> BaseModel:
    return cls.model_validate(data, by_name=True, by_alias=False)


#: (label, value, declared class, wire outcome, client outcome)
SHAPES: list[tuple[str, BaseModel, type[BaseModel], str, str]] = [
    ("AliasChoices first member collides with field b", ACcollide.model_construct(a=1, b=2), ACcollide, "REFUSED", "REFUSED"),
    ("AliasPath(p, 0)", APindex.model_validate({"p": [5]}), APindex, "EQUAL", "EQUAL"),
    ("AliasPath(p, 1)", APindex1.model_validate({"p": [0, 5]}), APindex1, "EQUAL", "EQUAL"),
    ("two AliasPaths share a prefix", APshared.model_validate({"p": {"a": 1, "b": 2}}), APshared, "EQUAL", "EQUAL"),
    ("AliasPath beside a plain alias", APplusAlias.model_validate({"p": {"a": 1}, "Y": 2}), APplusAlias, "EQUAL", "EQUAL"),
    ("populate_by_name with AliasChoices", Populatable(x=5), Populatable, "EQUAL", "EQUAL"),
    ("strict, AliasChoices datetime", StrictAC.model_validate({"W": T.replace(year=2030)}), StrictAC, "EQUAL", "EQUAL"),
    ("by-name outer, AliasPath inner", OuterByName(name="o", inner=InnerAP.model_validate({"p": {"a": 7}})), OuterByName, "EQUAL", "EQUAL"),
    ("field_serializer with AliasChoices", SerAC.model_validate({"xx": 5}), SerAC, "REFUSED", "REFUSED"),
    ("a subclass redeclares the alias, the handler declares the base", Sub9.model_validate({"z": 5}), Base9, "EQUAL", "EQUAL"),
    ("a subclass redeclares the alias, the handler declares the subclass", Sub9.model_validate({"z": 5}), Sub9, "LOST", "EQUAL"),
    ("serialize_by_alias, read by name", SerByAliasNameRead(x=5), SerByAliasNameRead, "EQUAL", "EQUAL"),
    ("serialize_by_alias, read by name, required", SerByAliasNameReq(x=5), SerByAliasNameReq, "EQUAL", "EQUAL"),
    ("an AliasPath head is another field's name", PrefixCollide.model_validate({"p": {"k": 1}}).model_copy(update={"x": 5}), PrefixCollide, "REFUSED", "REFUSED"),
    ("AliasChoices whose first member is a path", ACpathFirst.model_validate({"p": {"a": 5}}), ACpathFirst, "EQUAL", "EQUAL"),
    ("a non-idempotent validator with AliasChoices", BumpAC.model_validate({"xx": 5}), BumpAC, "EQUAL", "EQUAL"),
    ("a required AliasPath", APrequired.model_validate({"p": {"a": 5}}), APrequired, "EQUAL", "EQUAL"),
    ("AliasChoices whose later member is the name", ACsecond.model_validate({"q": 5}), ACsecond, "EQUAL", "EQUAL"),
    ("AliasChoices over a configuration-only base", ACoverAppModel.model_validate({"xx": 5}), ACoverAppModel, "EQUAL", "EQUAL"),
    ("AliasChoices over a defaults-only base", ACoverDefaults.model_validate({"xx": 5}), ACoverDefaults, "EQUAL", "EQUAL"),
    ("serialize_by_alias subclass, the handler declares the subclass", Sub9SerAlias.model_validate({"z": 5}), Sub9SerAlias, "REFUSED", "EQUAL"),
    ("two fields read from one key", _by_name(TwoFieldsOneKey, {"a": 1, "b": 2, "p": {"k": 3}}), TwoFieldsOneKey, "REFUSED", "REFUSED"),
    ("a subclass sent to a base that reads by name", ASub.model_validate({"xx": 5}), ABase, "EQUAL", "EQUAL"),
    ("a subclass sent to a base that reads its validation-alias form as other values", _by_name(MBSub, {"a": 38, "p": {"k": 4}, "b": 89}), MBBase, "REFUSED", "REFUSED"),
    ("the same, the handler declares the subclass", _by_name(MBSub, {"a": 38, "p": {"k": 4}, "b": 89}), MBSub, "REFUSED", "EQUAL"),
    ("a plain alias beside a validation alias", PSboth(X=1, yy=2), PSboth, "EQUAL", "EQUAL"),
]  # fmt: skip
