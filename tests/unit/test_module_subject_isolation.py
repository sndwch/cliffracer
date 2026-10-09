"""Two modules sharing one session prefix must not claim one subject space.

The session prefix separates *runs*. It does not separate modules inside a run,
and several integration modules declare a DLQ stream over the same subject
space under different names. A subject may be claimed by exactly one stream, so
under one prefix those become a single claim made repeatedly, and whichever
service starts second fails.

The module sub-token is what separates them. These tests pin that, and the
CONTROL pins that it is the sub-token doing the work rather than the prefix.
"""

from __future__ import annotations

import pytest

from cliffracer.core.subjects import subjects_overlap
from tests.broker_isolation import module_token, session_prefix

pytestmark = pytest.mark.unit

MOD_A = "tests.integration.test_jetstream_durable"
MOD_B = "tests.integration.test_live_concurrency_and_dlq"


def _dlq_claim(prefix: str) -> str:
    """The DLQ stream claim a module makes under *prefix*, as the tier spells it."""
    return f"{prefix}.dlq.*"


def test_two_modules_under_one_session_do_not_claim_one_subject_space():
    session = session_prefix()
    a = f"{session}_{module_token(MOD_A)}"
    b = f"{session}_{module_token(MOD_B)}"

    assert a != b, "two modules in one session resolved to the same prefix"
    assert not subjects_overlap(_dlq_claim(a), _dlq_claim(b)), (
        f"{_dlq_claim(a)} and {_dlq_claim(b)} overlap, so the second stream to "
        "declare one of them fails with 'subjects overlap with an existing stream'"
    )


def test_CONTROL_the_same_two_claims_under_one_prefix_do_overlap():
    """Without the sub-token the claims collide -- so the test above can fail."""
    session = session_prefix()
    assert subjects_overlap(_dlq_claim(session), _dlq_claim(session)), (
        "the control does not reproduce the collision, so the test above proves "
        "nothing about what the sub-token is for"
    )


def test_the_token_is_stable_for_one_module():
    assert module_token(MOD_A) == module_token(MOD_A)


def test_the_token_is_name_safe():
    """It has to render into a stream name and a durable, which take no '.'."""
    token = module_token(MOD_A)
    assert token, "empty token would collapse the prefix to the session's"
    assert all(c.isalnum() or c == "_" for c in token), token
