"""Pure NATS grants for a service's declared core messaging surface."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.subjects import subjects_overlap, validate_inbox_prefix, validate_subject
from cliffracer.introspect import Description, describe


def _patterns(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError("permission subjects must be a collection, not one string")
    return tuple(sorted({validate_subject(value) for value in values}))


@dataclass(frozen=True)
class BrokerPermissions:
    """Static subject grants and an optional finite-duration response grant: one reply per
    request, or, with `response_max=-1`, as many as a streamed reply sends."""

    publish: tuple[str, ...] = ()
    subscribe: tuple[str, ...] = ()
    response_ttl: float | None = None
    response_max: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "publish", _patterns(self.publish))
        object.__setattr__(self, "subscribe", _patterns(self.subscribe))
        if self.response_ttl is not None and (
            isinstance(self.response_ttl, bool)
            or not math.isfinite(self.response_ttl)
            or self.response_ttl <= 0
            or self.response_ttl > 9_223_372_036
        ):
            raise ValueError(
                "response_ttl must be finite, positive and within NATS's duration range"
            )
        if isinstance(self.response_max, bool) or not (
            self.response_max == -1 or self.response_max >= 1
        ):
            # NATS reads 0 as 1, so a 0 here would grant something other than it says.
            raise ValueError("response_max must be -1 (no limit) or a positive count of replies")

    def to_nats_permissions(self) -> dict[str, Any]:
        """Render a broker user's permissions; empty directions explicitly deny all."""
        result: dict[str, Any] = {
            "publish": {"allow": list(self.publish)} if self.publish else {"deny": [">"]},
            "subscribe": {"allow": list(self.subscribe)} if self.subscribe else {"deny": [">"]},
        }
        if self.response_ttl is not None:
            result["allow_responses"] = {
                "max": self.response_max,
                "expires": f"{math.ceil(self.response_ttl * 1_000_000_000)}ns",
            }
        return result


def _description(contract: type | Description, config: ServiceConfig) -> Description:
    description = describe(contract, config=config) if isinstance(contract, type) else contract
    if not isinstance(description, Description):
        raise TypeError("expected a service class or Description")
    if description.service != config.name:
        raise ValueError("description.service must match config.name")
    return description


def _resource_name(name: str) -> str:
    validate_subject(name, wildcards=False)
    if any(char in name for char in ".\\/"):
        raise ValueError(f"stream and consumer names must be single resource tokens: {name!r}")
    return name


def permission_subject(prefix: str, *parts: str) -> str:
    """Join reserved API or explicitly scoped inbox/resource permission segments."""
    return validate_subject(".".join((prefix, *parts)))


def _jetstream_permissions(
    description: Description, config: ServiceConfig, publish: set[str]
) -> None:
    streams = config.effective_jetstream_streams
    binding = config.jetstream_resource_mode == "bind"
    if streams and not binding:
        publish.add("$JS.API.STREAM.LIST")
    for stream in streams:
        name = _resource_name(stream.name)
        if binding:
            publish.add(permission_subject("$JS.API.STREAM.INFO", name))
        else:
            publish.add(permission_subject("$JS.API.STREAM.CREATE", name))
            if config.jetstream_update_streams:
                publish.add(permission_subject("$JS.API.STREAM.UPDATE", name))
        publish.update(_patterns(stream.subjects))

    for listener in description.listeners:
        if not listener.durable:
            continue
        subject = HandlerDiscovery.effective_event_subject(
            config, listener.pattern, listener.cross_namespace
        )
        matches = [
            stream
            for stream in streams
            if any(subjects_overlap(pattern, subject) for pattern in stream.subjects)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"listener {listener.handler_name!r} needs exactly one declared stream for "
                f"{subject!r}; found {[stream.name for stream in matches]}"
            )
        stream_name = _resource_name(matches[0].name)
        durable = _resource_name(config.prefixed_name(listener.durable))
        if not binding:
            publish.add("$JS.API.STREAM.NAMES")
        publish.add(permission_subject("$JS.API.CONSUMER.INFO", stream_name, durable))
        if listener.pull:
            if not binding:
                # An all-subject filter uses the named, unfiltered consumer endpoint.
                filter_tokens = () if subject == ">" else (subject,)
                publish.add(
                    permission_subject(
                        "$JS.API.CONSUMER.CREATE", stream_name, durable, *filter_tokens
                    )
                )
            publish.add(permission_subject("$JS.API.CONSUMER.MSG.NEXT", stream_name, durable))
        elif not binding:
            publish.add(permission_subject("$JS.API.CONSUMER.DURABLE.CREATE", stream_name, durable))
        publish.add(permission_subject("$JS.ACK", stream_name, durable, ">"))
        publish.add(permission_subject("$JS.ACK.*.*", stream_name, durable, ">"))


def _refuse_a_grant_over_the_roles_own_subjects(
    prefix: str, config: ServiceConfig, publish: set[str], subscribe: set[str]
) -> None:
    """Refuse an inbox prefix whose subscribe grant covers subjects the role itself uses.

    The inbox grant is `<prefix>.>`, a subscribe permission over everything beneath the prefix.
    A prefix that sits in the service's own subject space makes it a blanket subscribe over that
    space and not an inbox: the environment prefix, the namespace, or any subject the role is
    granted. A dedicated prefix anywhere else, `_INBOX.orders` or `orders.replies`, is not.
    """
    grant = permission_subject(prefix, ">")
    scope = (
        permission_subject(config.subject_prefix, ">")
        if config.subject_prefix
        else permission_subject(config.namespace, ">")
        if config.namespace
        else None
    )
    if scope is not None and subjects_overlap(grant, scope):
        raise ValueError(
            f"inbox_prefix {prefix!r} is inside the service's own subject space: its grant "
            f"{grant!r} overlaps {scope!r}, the subjects of its environment. Choose a prefix "
            f"outside it, such as '_INBOX.{config.name}'"
        )
    covered = sorted(subject for subject in publish | subscribe if subjects_overlap(grant, subject))
    if covered:
        raise ValueError(
            f"inbox_prefix {prefix!r} overlaps subjects this role uses: its grant {grant!r} "
            f"covers {covered}. Choose a prefix outside them, such as '_INBOX.{config.name}'"
        )


def broker_permissions(
    contract: type | Description,
    config: ServiceConfig,
    *,
    role: Literal["service", "client"],
    inbox_prefix: str | None = None,
    allow_async: bool = False,
    response_ttl: float | None = 30.0,
    extra_publish: Iterable[str] = (),
    extra_subscribe: Iterable[str] = (),
) -> BrokerPermissions:
    """Derive core grants without opening a connection or constructing a service.

    The inbox prefix is mandatory, supplied explicitly or on the service config. It is refused
    when its grant would cover the role's own subject space (the environment prefix, the
    namespace or a subject the role is granted), which would make it a blanket subscribe.
    Extra subjects are already wire-scoped. Outbound application calls and
    extension traffic require explicit grants; their destinations are not in
    the class contract. JetStream provisioning grants assume trusted code.
    """
    if role not in {"service", "client"}:
        raise ValueError("role must be 'service' or 'client'")
    prefix = inbox_prefix
    if prefix is None and role == "service":
        prefix = config.nats_inbox_prefix
    if prefix is None:
        raise ValueError("a dedicated inbox_prefix is required for broker permissions")
    validate_inbox_prefix(prefix)
    if role == "service" and config.nats_inbox_prefix != prefix:
        raise ValueError("set config.nats_inbox_prefix to the service permission inbox_prefix")
    description = _description(contract, config)
    publish = set(_patterns(extra_publish))
    subscribe = set(_patterns(extra_subscribe))
    subscribe.add(permission_subject(prefix, ">"))
    describe_subject = HandlerDiscovery.with_namespace(config, f"{config.name}.describe")
    if role == "client":
        publish.add(describe_subject)
        for method in description.methods:
            _resource_name(method.name)
            for verb in ("rpc", "async") if allow_async else ("rpc",):
                publish.add(
                    HandlerDiscovery.outbound_subject(config, config.name, verb, method.name)
                )
        _refuse_a_grant_over_the_roles_own_subjects(
            prefix, config, publish, subscribe - {permission_subject(prefix, ">")}
        )
        return BrokerPermissions(tuple(publish), tuple(subscribe))

    subscribe.add(describe_subject)
    for verb in ("rpc", "async"):
        subscribe.add(HandlerDiscovery.outbound_subject(config, config.name, verb, "*"))
    for listener in description.listeners:
        # Durable JetStream messages arrive on inbox subscriptions, not on the filter subject.
        if not (config.jetstream_enabled and listener.durable):
            subscribe.add(
                HandlerDiscovery.effective_event_subject(
                    config, listener.pattern, listener.cross_namespace
                )
            )
    publish.add(HandlerDiscovery.dlq_subject(config))
    if config.jetstream_enabled:
        _jetstream_permissions(description, config, publish)
    _refuse_a_grant_over_the_roles_own_subjects(
        prefix, config, publish, subscribe - {permission_subject(prefix, ">")}
    )
    max_replies = _response_max(description, config, response_ttl)
    _refuse_a_grant_shorter_than_the_deadline_reply(description, config, response_ttl)
    return BrokerPermissions(tuple(publish), tuple(subscribe), response_ttl, max_replies)


#: Seconds a service's response grant must outlast `max_rpc_processing_time`. The grant's clock
#: starts when the broker routes the request, before the service's deadline does, and the reply a
#: service sends at its deadline (`deadline_exceeded`, or the envelope ending a stream cut there) is
#: published just after it: past a grant of exactly the bound, the broker refuses that reply and
#: the caller times out instead. Measured on loopback, the reply reached its caller at most 13 ms
#: past the bound under a full test run; the rest is room for a broker a network hop away.
RESPONSE_GRANT_MARGIN = 1.0


def _refuse_a_grant_shorter_than_the_deadline_reply(
    description: Description, config: ServiceConfig, ttl: float | None
) -> None:
    """Refuse a response grant that expires before the reply a service sends at its deadline.

    Nothing is refused without a grant (`ttl` None) or without a deadline of the service's own
    (`max_rpc_processing_time` None): the grant then bounds the reply on its own terms.
    """
    cap = config.max_rpc_processing_time
    if ttl is None or cap is None or ttl >= cap + RESPONSE_GRANT_MARGIN:
        return
    streaming = sorted(m.name for m in description.methods if m.returns.get("kind") == "stream")
    lost = "a deadline_exceeded reply"
    if streaming:
        lost += f" and the end of a stream of {', '.join(streaming)}"
    raise ValueError(
        f"response_ttl={ttl}s does not cover max_rpc_processing_time={cap}s plus the "
        f"{RESPONSE_GRANT_MARGIN}s margin the reply sent at the deadline needs: the broker would "
        f"drop {lost} without a word, and the caller would time out instead; raise "
        f"response_ttl to at least {cap + RESPONSE_GRANT_MARGIN}"
    )


def _response_max(description: Description, config: ServiceConfig, ttl: float | None) -> int:
    """How many replies the service's response grant allows a request: one, or no limit when a
    method streams its reply. A stream's replies are granted for `ttl`, and the broker drops
    those after it with no word to either side, so `max_rpc_processing_time` must bound every
    stream; `_refuse_a_grant_shorter_than_the_deadline_reply` holds `ttl` to that bound."""
    streaming = sorted(m.name for m in description.methods if m.returns.get("kind") == "stream")
    if not streaming or ttl is None:
        return 1
    if config.max_rpc_processing_time is None:
        raise ValueError(
            f"{config.name} streams the reply of {', '.join(streaming)}: set "
            f"max_rpc_processing_time, which bounds a stream, so the response grant of "
            f"response_ttl={ttl}s can cover it"
        )
    return -1


__all__ = ["RESPONSE_GRANT_MARGIN", "BrokerPermissions", "broker_permissions"]
