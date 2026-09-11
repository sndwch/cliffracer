"""Generate a typed client from a service description."""

from .emitter import CannotEmit, annotation_text, emit, imports_for

__all__ = ["CannotEmit", "annotation_text", "emit", "imports_for"]
