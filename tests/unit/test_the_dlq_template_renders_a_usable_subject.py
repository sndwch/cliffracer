"""A dead-letter template is refused when it cannot render a usable subject.

`dlq_subject` is the only subject on a `ServiceConfig` that is a *template*, so
it is the only one whose validity depends on the config's other fields. A
template naming `{namespace}` on a config without one renders an empty token --
`.dlq.orders` -- which NATS refuses.

WHY AT CONFIG TIME. This subject is used only when a message is already being
dead-lettered, so the first message to exercise a wrong template is one that was
going to be dropped anyway, now also failing to be recorded. The broker says
`nats: invalid subject`, which names neither the template nor the field that
produced it.

The validator does not re-implement the rule: it renders the template and hands
the result to `_unusable_subject_reason`, the same function that already guards
decorator subjects and the three outbound publish paths. `test_the_verdict_is
_the_shared_checkers` pins that, so this cannot drift into a second opinion
about what a subject is.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cliffracer import ServiceConfig
from cliffracer.core.decorators import _unusable_subject_reason
from cliffracer.core.discovery import HandlerDiscovery

pytestmark = pytest.mark.unit


def _config(template: str, *, namespace: str | None = None, prefix: str | None = None):
    kwargs: dict[str, object] = {
        "name": "orders",
        "health_port": 0,
        "health_listener": False,
        "dlq_subject": template,
    }
    if namespace is not None:
        kwargs["namespace"] = namespace
    if prefix is not None:
        kwargs["subject_prefix"] = prefix
    return ServiceConfig(**kwargs)  # type: ignore[arg-type]


# --- refused, in both positions ----------------------------------------------


@pytest.mark.parametrize(
    ("template", "rendered"),
    [
        ("{namespace}.dlq.{service}", ".dlq.orders"),
        ("dlq.{service}.{namespace}", "dlq.orders."),
        ("{namespace}.dlq.{service}.{namespace}", ".dlq.orders."),
    ],
    ids=["leading", "trailing", "both ends"],
)
def test_a_namespace_token_with_no_namespace_is_refused(template, rendered):
    """The trailing form matters as much as the leading one: same defect, other end."""
    with pytest.raises(ValidationError) as caught:
        _config(template)

    message = str(caught.value)
    assert template in message, message
    assert rendered in message, f"the message does not show what it rendered: {message}"
    assert "empty token" in message, message


def test_the_refusal_says_what_to_do_about_it():
    """A reason alone leaves the reader to work out which half to change."""
    with pytest.raises(ValidationError) as caught:
        _config("{namespace}.dlq.{service}")

    message = str(caught.value)
    assert "this config has none" in message, message
    assert "set `namespace`" in message and "drop {namespace}" in message, message


def test_a_placeholder_the_config_cannot_fill_is_refused_by_name():
    """`{svc}` used to raise KeyError from inside the dead-letter path.

    Rendering the template at config time means this has to be handled here, and
    naming the placeholder is strictly better than a bare KeyError at the moment
    a message dies.
    """
    with pytest.raises(ValidationError) as caught:
        _config("dlq.{svc}")

    message = str(caught.value)
    assert "'svc'" in message, message
    assert "{service}" in message and "{namespace}" in message, message


def test_a_template_that_is_invalid_on_its_own_is_refused_too():
    """The check reads the RENDERED subject, so it is not a {namespace} special case.

    A doubled dot has nothing to do with the namespace and is refused for the
    same reason by the same function.
    """
    with pytest.raises(ValidationError) as caught:
        _config("dlq..{service}")

    assert "dlq..orders" in str(caught.value)


# --- accepted, and unchanged --------------------------------------------------


def test_CONTROL_the_default_template_is_accepted():
    """The default names no namespace, so a default service must be unaffected."""
    config = ServiceConfig(name="orders", health_port=0, health_listener=False)

    assert HandlerDiscovery.dlq_subject(config) == "dlq.orders"


@pytest.mark.parametrize(
    ("template", "namespace", "prefix", "expected"),
    [
        ("{namespace}.dlq.{service}", "appA", None, "appA.dlq.orders"),
        ("dlq.{service}.{namespace}", "appA", None, "dlq.orders.appA"),
        ("dlq.{service}", None, "w7", "w7.dlq.orders"),
        ("{namespace}.dlq.{service}", "appA", "w7", "w7.appA.dlq.orders"),
        ("custom.dlq", None, None, "custom.dlq"),
    ],
    ids=["namespaced", "namespaced trailing", "prefixed", "both", "no placeholder"],
)
def test_CONTROL_a_template_that_renders_a_subject_is_accepted(
    template, namespace, prefix, expected
):
    """The refusal must be about the empty token, not about naming {namespace}.

    A `{namespace}` template is perfectly good on a config that has one, and
    this is the row that stops the fix being "reject {namespace} templates".
    """
    config = _config(template, namespace=namespace, prefix=prefix)

    assert HandlerDiscovery.dlq_subject(config) == expected


# --- the validator and the builder cannot disagree ----------------------------


@pytest.mark.parametrize(
    ("template", "namespace", "prefix"),
    [
        ("dlq.{service}", None, None),
        ("{namespace}.dlq.{service}", None, None),
        ("dlq.{service}.{namespace}", None, None),
        ("{namespace}.dlq.{service}", "appA", None),
        ("dlq.{service}", None, "w7"),
        ("{namespace}.dlq.{service}", "appA", "w7"),
        ("dlq..{service}", None, None),
        ("custom.dlq", None, None),
    ],
)
def test_the_verdict_is_the_shared_checkers(template, namespace, prefix):
    """A config is accepted exactly when the builder's output is a usable subject.

    This is the assertion that keeps the two from drifting: it does not restate
    the rule, it compares the validator's decision against
    `_unusable_subject_reason` applied to what `HandlerDiscovery.dlq_subject`
    actually returns. If either side gains a rule the other lacks, this fails.
    """
    try:
        config = _config(template, namespace=namespace, prefix=prefix)
    except ValidationError:
        accepted = False
    else:
        accepted = True

    if accepted:
        built = HandlerDiscovery.dlq_subject(config)
        assert _unusable_subject_reason(built) is None, (
            f"config was accepted but the builder produces {built!r}, which the "
            f"shared checker refuses: {_unusable_subject_reason(built)}"
        )


def test_CONTROL_the_shared_checker_still_refuses_the_shapes_this_relies_on():
    """If this ever passes clean, the validator above is asserting nothing.

    The whole fix is "hand the rendered subject to the existing checker", so the
    checker refusing these is the premise, not an implementation detail.
    """
    assert _unusable_subject_reason(".dlq.orders") is not None
    assert _unusable_subject_reason("dlq.orders.") is not None
    assert _unusable_subject_reason("dlq..orders") is not None
    assert _unusable_subject_reason("dlq.orders") is None
