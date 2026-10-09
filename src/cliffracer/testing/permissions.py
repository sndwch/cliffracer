"""Named contract checks for broker grants kept by an application."""

from typing import Literal

from cliffracer.broker_permissions import BrokerPermissions
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.subjects import subject_matches
from cliffracer.introspect import Description, describe


def assert_rpc_permissions(
    contract: type | Description,
    config: ServiceConfig,
    permissions: BrokerPermissions,
    *,
    role: Literal["service", "client"] = "client",
    allow_async: bool = False,
) -> None:
    """Name every declared RPC missing from the selected static grant direction.

    This checks named calls and discovery. It does not certify a whole broker
    policy, dynamic response grants, extensions or application outbound traffic.
    """
    if role not in {"service", "client"}:
        raise ValueError("role must be 'service' or 'client'")
    description = describe(contract, config=config) if isinstance(contract, type) else contract
    if description.service != config.name:
        raise ValueError("description.service must match config.name")
    subjects = permissions.publish if role == "client" else permissions.subscribe
    required = {"describe": HandlerDiscovery.with_namespace(config, f"{config.name}.describe")}
    for method in description.methods:
        for verb in ("rpc", "async") if allow_async else ("rpc",):
            required[f"{verb} {method.name}"] = HandlerDiscovery.outbound_subject(
                config, config.name, verb, method.name
            )
    missing = [
        f"{name}: {subject}"
        for name, subject in required.items()
        if not any(subject_matches(pattern, subject) for pattern in subjects)
    ]
    if missing:
        # Raised, not asserted: this is shipped code, and an `assert` is removed by `python -O`,
        # which would leave a check an application keeps in its suite unable to fail.
        raise AssertionError(f"{role} permissions omit registered calls: " + "; ".join(missing))
