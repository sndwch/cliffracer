"""A payload that validation refuses counts in `permitted`, and the docs and the code say so.

Validation runs after every declared extension, so a rate limit has let a payload through before
validation refuses it. `rate_limits` on `/health` then counts it as permitted. Two sentences said
such a payload "counts in neither" after the order changed: the extensions guide and the docstring
of `health_details`. The README of the package was right. This runs the case and reads the sentences.
"""

import json
from pathlib import Path

import pytest
from cliffracer_resilience import ResilienceExtension, rate_limit
from cliffracer_resilience.extension import ResilienceExtension as Extension

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing import MockMessage

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


class Limited(CliffracerService):
    resilience = ResilienceExtension()

    @rpc
    @rate_limit(calls=1, window=60.0)
    async def ok(self, x: int) -> int:
        return x


async def _call(svc: Limited, body: dict) -> dict:
    message = MockMessage(
        "s.rpc.ok",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        reply="_INBOX.r",
    )
    await svc.container._handle_rpc_request(message)
    return json.loads(message.responded_data)


async def test_a_payload_validation_refuses_is_counted_as_permitted():
    svc = Limited(ServiceConfig(name="s", subject_prefix=None, health_port=0))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    assert (await _call(svc, {"x": "a"}))["code"] == "validation_failed"
    assert (await _call(svc, {"x": 1}))["code"] == "refused"

    counts = svc.resilience.health_details()["rate_limits"]
    assert counts["by_handler"]["ok"] == {"permitted": 1, "refused": 1}


def test_the_guide_and_the_docstring_say_it_counts_in_permitted():
    guide = " ".join((REPO / "docs" / "extensions.md").read_text().split())
    docstring = " ".join((Extension.health_details.__doc__ or "").split())

    for text in (guide, docstring):
        assert "counts in `permitted`" in text, text[:200]
        assert "counts in neither" not in text and "is in neither" not in text
