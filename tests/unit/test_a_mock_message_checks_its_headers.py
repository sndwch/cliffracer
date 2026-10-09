"""A wrong `headers` argument is reported where it was passed, not where it is used.

`MockMessage(subject, data, headers, reply)` puts `headers` third and `reply`
fourth, and a reply subject is the thing a caller most often wants to pass. So
three positional arguments bind a string to `headers` silently, and the message
says nothing until `respond` copies them:

    ValueError: dictionary update sequence element #0 has length 1; 2 is required

That names neither the argument nor the call that supplied it, and it is raised
from a method the caller did not think they were misusing. `MockMessage` is
exported from `cliffracer.testing`, so the person who trips this is a USER of
the testing helpers rather than this suite.

NO LIVE INSTANCE, and the issue that reported one retracted it. Measured by AST
over the whole tree: 24 `MockMessage` constructions, **0** passing three or more
positional arguments -- `tests/conftest.py` passes `reply=` by keyword. This is
a footgun on a public API rather than a bug in the suite, which is why it is
fixed at the cheapest point rather than by making the signature keyword-only:
that would be a breaking change for callers outside this tree, to prevent a
mistake the error below already reports clearly.
"""

from __future__ import annotations

import typing
from collections.abc import Mapping
from types import MappingProxyType

import pytest

from cliffracer.testing.messages import MockMessage

pytestmark = pytest.mark.unit


def test_a_reply_subject_in_the_headers_position_is_refused_at_construction():
    """The actual mistake, reported at the call that makes it."""
    with pytest.raises(TypeError) as caught:
        MockMessage("orders.created", b"{}", "_INBOX.test")

    message = str(caught.value)
    assert "headers" in message, message
    assert "_INBOX.test" in message, message
    assert "str" in message, message


def test_the_message_says_which_position_and_what_to_do_instead():
    """A message that names the type and not the trap leaves the reader stuck.

    The reason this happens at all is positional: third versus fourth. Saying so
    is the difference between "that was the wrong type" and "you meant `reply`".
    """
    with pytest.raises(TypeError) as caught:
        MockMessage("orders.created", b"{}", "_INBOX.test")

    message = str(caught.value)
    assert "reply" in message, message
    assert "keyword" in message.lower() or "reply=" in message, message


@pytest.mark.parametrize(
    ("label", "headers"),
    [
        ("a dict", {"X-Correlation-ID": "abc"}),
        ("an empty dict", {}),
        ("None", None),
        ("any Mapping, not only dict", MappingProxyType({"a": "b"})),
    ],
)
def test_CONTROL_a_mapping_or_none_is_accepted(label, headers):
    """Otherwise "refuses everything" would satisfy the tests above.

    `respond` copies with `dict(...)`, which takes any mapping, so the check is
    `Mapping` rather than `dict` -- a `MappingProxyType` worked before this and
    must keep working.
    """
    msg = MockMessage("orders.created", b"{}", headers)

    assert msg.headers == (dict(headers) if headers is not None else {})


async def test_CONTROL_respond_still_works_on_an_accepted_message():
    """The method the old failure came from, on the path that is still valid."""
    msg = MockMessage("orders.created", b"{}", {"X-Correlation-ID": "abc"}, "_INBOX.test")

    await msg.respond(b'{"ok": true}')

    assert msg.responded_data == b'{"ok": true}'
    assert msg.response_headers == {"X-Correlation-ID": "abc"}


def test_CONTROL_the_keyword_form_the_suite_uses_is_unaffected():
    """`tests/conftest.py` spells it this way; nothing here may break it."""
    msg = MockMessage("orders.created", b"{}", reply="_INBOX.test")

    assert msg.reply == "_INBOX.test"
    assert msg.headers == {}


def test_the_headers_are_copied_rather_than_aliased():
    """A caller mutating its own dict afterwards must not change the message.

    Not the reported defect, but the check that makes the argument a value: the
    constructor now takes `dict(headers)`, and asserting it means a later
    change back to storing the caller's object reds here rather than surfacing
    as a message whose headers changed under a test.
    """
    supplied = {"X-Correlation-ID": "abc"}
    msg = MockMessage("orders.created", b"{}", supplied)

    supplied["X-Correlation-ID"] = "mutated"

    assert msg.headers == {"X-Correlation-ID": "abc"}


def test_the_annotation_is_as_wide_as_the_check():
    """The signature must not reject what the body accepts.

    `respond` copies with `dict(...)`, which takes any mapping, so the check is
    `Mapping`. If the ANNOTATION says `dict`, a type-checking caller gets an
    error for the exact case `test_CONTROL_a_mapping_or_none_is_accepted`
    asserts works — the `isinstance(dict)` mutation surviving at the type level
    instead of in the body.

    **Nothing in this repository's gates can see that.** `mypy` runs against
    `src/` and `packages/*/src`, so a test proving `MappingProxyType` works is
    not type-checked, and the caller who meets the contradiction is outside this
    tree, because `cliffracer.testing` is exported. It was found by
    type-checking a caller from that position.

    So the fence is here, reading the annotation rather than trusting a type
    checker that never looks at it.
    """
    hints = typing.get_type_hints(MockMessage.__init__)
    headers = hints["headers"]

    args = typing.get_args(headers)
    assert args, f"expected an optional type, got {headers!r}"
    non_none = [a for a in args if a is not type(None)]
    assert len(non_none) == 1, non_none
    origin = typing.get_origin(non_none[0]) or non_none[0]

    assert origin is Mapping, (
        f"headers is annotated {origin!r}; it must be as wide as the isinstance "
        f"check, or a type-checking caller is refused the case the runtime accepts"
    )
