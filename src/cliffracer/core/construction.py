"""Building a service from a service class and an optional config.

The runner, the CLI and the test harness all turn a service class into an
instance, so they share one answer to what a constructor accepts and one set of
overlay rules.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, cast

from pydantic import ValidationError

from .service import CliffracerService
from .service_config import ServiceConfig

# The kinds a single positional argument can bind to. A keyword-only parameter
# and a ``**kwargs`` catch-all each accept no positional argument, so neither
# makes a constructor one that can be handed a config.
_POSITIONAL_KINDS = frozenset(
    {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.VAR_POSITIONAL,
    }
)


def constructor_takes_config(service_class: type[CliffracerService]) -> bool:
    """True if the service class constructor accepts a positional config arg."""
    try:
        sig = inspect.signature(service_class)
    except (TypeError, ValueError):
        return False
    return any(param.kind in _POSITIONAL_KINDS for param in sig.parameters.values())


def apply_config_overlay(service: CliffracerService, values: dict[str, Any]) -> None:
    """Overlay config field values onto a constructed service's config.

    The overlay is validated as one config, and nothing is applied if it is refused. Assigning the
    fields one at a time revalidated the whole model at each assignment, so a pair that is valid
    together and not alone (`nats_user` with `nats_password`, `jetstream_resource_mode='bind'` with
    `jetstream_update_streams=False` over a config that has it on) failed at its first field
    whichever order it was written in, and a refusal part way through left the config half changed.

    The config object is changed in place, not replaced: the container and the dispatchers hold it.
    """
    config = service.config
    for key in values:
        if not hasattr(config, key):
            raise ValueError(f"Unknown ServiceConfig field in overrides: {key!r}")
    # What the config already sets, and no more, so a field it left to its default stays unset.
    merged = type(config).model_validate({**config.model_dump(exclude_unset=True), **values})
    for key in values:
        # Not `setattr`: that revalidates the whole model for each field, which is what the
        # validation above has already done once for all of them.
        object.__setattr__(config, key, getattr(merged, key))
        config.__pydantic_fields_set__.add(key)


def overlay_refusal_text(refusal: ValueError) -> str:
    """Why an overlay was refused, as the fields and reasons and never the values refused.

    A `ValidationError` prints the input it refused, and an overlay holds credentials (a YAML
    `nats_password`, the password in a `nats_url`), so the text is built from each error's
    location and message only.
    """
    if not isinstance(refusal, ValidationError):
        return str(refusal)
    reasons = []
    for error in refusal.errors():
        where = ".".join(str(part) for part in error["loc"])
        reasons.append(f"{where}: {error['msg']}" if where else error["msg"])
    return "; ".join(reason.replace("Value error, ", "", 1) for reason in reasons)


def construct_service(
    service_class: type[CliffracerService], config: ServiceConfig | None = None
) -> CliffracerService:
    """Construct a service, honouring both constructor conventions.

    Self-configuring services (no-arg ``__init__``) are the convention; the
    instance builds its own ``config``. A constructor that accepts a positional
    config arg is handed ``config`` instead. A ``config`` given for a no-arg
    class is overlaid onto the instance's own config, minus ``name``, which
    stays the service's own.
    """
    if config is not None and constructor_takes_config(service_class):
        return service_class(config)
    service = cast(Callable[[], CliffracerService], service_class)()
    if config is not None:
        # exclude_unset (not exclude_defaults): an explicitly-passed value wins
        # even when it equals the schema default.
        overlay = config.model_dump(exclude_unset=True)
        overlay.pop("name", None)
        apply_config_overlay(service, overlay)
    return service
