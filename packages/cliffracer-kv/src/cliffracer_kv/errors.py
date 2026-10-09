"""Exceptions for cliffracer-kv."""

from __future__ import annotations


class KvError(Exception):
    """Base for the errors cliffracer-kv raises itself.

    Errors nats-py raises propagate unchanged: a stale revision, a bucket the broker refuses to
    create, a missing bucket when `create_if_missing` is off.
    """


class JetStreamUnavailableError(KvError):
    """Raised when JetStream is required by KvExtension but is not available."""


class BucketConfigError(KvError):
    """Raised when bucket or object store configuration is invalid."""


class ModelDoesNotReadBackError(KvError, TypeError):
    """Raised by a write when no form a model can be stored in reads back as itself.

    A model is stored as its JSON under its field names, under its aliases, or with each field
    where its validation alias reads it, whichever its class, and each base class declaring its
    fields, reads back equal to the model. Where no form serves every class, it is stored in the
    form earlier releases stored, if its own class reads that back as the model, else in one its
    own class reads back and no base class reads worse than that form. When none of these exists
    (a serializer that changes the value, a base class that reads the field by name while the
    model reads it only through its validation alias), `get(as_type=...)` would return other
    values than were written, or refuse the bytes, so nothing is written. A `TypeError` too, as
    every value a write cannot store is.
    """
