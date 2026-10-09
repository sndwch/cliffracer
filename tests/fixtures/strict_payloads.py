"""Strict models and a service that takes them, for the tests of a strict model's payload."""

import datetime
import decimal
import uuid

from pydantic import BaseModel, ConfigDict, TypeAdapter

from cliffracer import CliffracerService, listener, rpc

WHEN = datetime.datetime(2026, 1, 2, 3, 4, 5)


class Record(BaseModel):
    """Strict, with the types whose JSON form is not their python form."""

    model_config = ConfigDict(strict=True)

    when: datetime.datetime
    ident: uuid.UUID
    amount: decimal.Decimal
    tags: set[str]
    pair: tuple[int, int]
    count: int


class Blob(BaseModel):
    """Strict, holding bytes: a foreign msgpack producer's `bin` is python `bytes`."""

    model_config = ConfigDict(strict=True)

    raw: bytes


class Stamped(BaseModel):
    model_config = ConfigDict(strict=True)

    raw: bytes
    when: datetime.datetime


RECORD = Record(
    when=WHEN,
    ident=uuid.UUID(int=7),
    amount=decimal.Decimal("1.5"),
    tags={"t"},
    pair=(3, 4),
    count=2,
)
DUMP = TypeAdapter(Record).dump_python(RECORD, mode="json")


class Records(CliffracerService):
    received: list = []

    @rpc
    async def put(self, record: Record) -> str:
        self.received.append(record)
        return str(record.ident)

    @rpc
    async def blob(self, blob: Blob) -> int:
        self.received.append(blob)
        return len(blob.raw)

    @listener("records.put", fanout=True)
    async def on_put(self, record: Record) -> None:
        self.received.append(record)

    @listener("records.batch", fanout=True)
    async def on_batch(self, records: list[Record]) -> None:
        self.received.append(records)
