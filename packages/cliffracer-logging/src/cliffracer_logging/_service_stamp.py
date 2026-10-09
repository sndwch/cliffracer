"""The marker that tells the service a configure or a setup stamped on every record from one a call bound.

`LoggingConfig.configure` and `setup_correlation_logging` store the service name in loguru's
process-wide `extra`, and loguru merges that into every record before a sink's filter runs, so a
line nothing bound a service to carries the name of the last service configured. A filter that asks
"is this record's `service` mine" cannot tell that line from one a call bound with
`logger.bind(service=...)`.

The stamp is therefore an instance of `ProcessService`, a `str` that formats and serialises as the
name it holds. loguru merges `{**extra, **bound}` and keeps each value as the object it was, through
a filter, through an `enqueue=True` sink (the record is pickled there, so the class is module-level)
and through `serialize=True`, so a filter can ask whether a record's `service` is the stamp. A value
a call bound is the plain `str` it passed.
"""

from __future__ import annotations

from typing import Any, cast

from loguru import logger


class ProcessService(str):
    """The process-wide `service`: a name stamped on every record, not bound by the line's author."""

    __slots__ = ()


def is_bound_service(value: object) -> bool:
    """Whether `value`, a record's `service`, was bound by a call and is not the process-wide stamp.

    By type, not by name: a bound value equal to the stamp's name is bound. A call that binds the
    stamp object itself (`logger.bind(service=extra["service"])`) passes the marker along, and is
    read as the stamp; `str(value)` is the plain string.
    """
    return isinstance(value, str) and not isinstance(value, ProcessService)


def global_extra() -> dict[str, Any]:
    """The ``extra`` dict every record carries, which loguru offers no public way to read."""
    return dict(cast(Any, logger)._core.extra)


def claim_process_service(
    service_name: str, *, replace_existing: bool, what: str, set_when_unset: bool
) -> None:
    """Set the process-wide `service`, or keep the one another service named and say so.

    A call that replaces the process's sinks owns its logging, so the process-wide `service` becomes
    its own, merged into the existing `extra` (the host's other context stays). A call that adds next
    to existing sinks leaves it: the earlier service's sinks stay installed, and the label cannot be
    two names. When it names another service, one WARNING names the kept and the ignored service.
    `set_when_unset` says whether such a call sets the `service` when none is named yet.
    """
    if replace_existing:
        logger.configure(extra={**global_extra(), "service": ProcessService(service_name)})
        return
    kept = global_extra().get("service")
    if kept is None:
        if set_when_unset:
            logger.configure(extra={**global_extra(), "service": ProcessService(service_name)})
    elif kept != service_name:
        logger.warning(
            f"{what} for service {service_name!r} was added next to existing sinks, and the "
            f"process-wide service is {kept!r}: lines written through the plain logger keep "
            f"service={kept!r}, and {service_name!r} is ignored"
        )
