"""A generated client continues the trace it was called inside.

`_headers_for_send` looked for a correlation id in `self.headers` -- a dict
fixed at construction -- and otherwise minted `uuid.uuid4().hex`. It never
consulted `CorrelationContext`. So a service handling an inbound request and
calling a peer through a generated client sent a brand-new id, and the trace
chain broke at that hop.

MEASURED BEFORE THE FIX: with `CorrelationContext.set("inbound-trace-abc")`, the
id on the wire was `3e97363f32054915acd36990dea4d469`.

THE SERVICE'S OWN OUTBOUND PATH ALREADY DID THIS. `call_rpc` and
`publish_event` resolve `CorrelationContext.get() or get_or_create_id()`, so two
supported ways of calling the same peer disagreed, with nothing in the docs
saying which preserved a trace.

PRECEDENCE FOLLOWS `publish_event`, which resolves an explicit
`correlation_id` kwarg first, then the ambient id, then a new one. So an
explicit constructor header still wins here: it is a deliberate instruction and
this change is not the place to overrule it. Measured before choosing --
nothing in the tree constructs a `ServiceClient` with a correlation header, and
the option is undocumented, so either order was safe and consistency with the
service side decided it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cliffracer import CorrelationContext
from cliffracer.client import ServiceClient

pytestmark = pytest.mark.unit


class Recorder:
    """The headers each request carried, in order."""

    def __init__(self) -> None:
        self.headers: list[dict[str, str]] = []
        self.subjects: list[str] = []

    async def request(self, subject, payload, headers=None):
        self.subjects.append(subject)
        self.headers.append(dict(headers or {}))
        if subject.endswith("describe"):
            return SimpleNamespace(
                data=json.dumps(
                    {
                        "service": "svc",
                        "version": "1",
                        "description_hash": "sha256:d",
                        "methods": [
                            {
                                "name": "do",
                                "signature_hash": "sha256:x",
                                "params": [],
                                "returns": None,
                            }
                        ],
                    }
                ).encode(),
                headers=None,
            )
        return SimpleNamespace(data=b'{"success":true,"result":1}', headers=None)

    @property
    def ids(self) -> list[str]:
        return [h["X-Correlation-ID"] for h in self.headers]


def _client(recorder: Recorder, **kw) -> ServiceClient:
    client = ServiceClient(service="svc", verify=False, **kw)
    client._nc = AsyncMock()
    client._request = recorder.request  # type: ignore[method-assign]
    return client


@pytest.fixture(autouse=True)
def _no_ambient_id_leaks_between_tests():
    yield
    CorrelationContext.clear()


async def test_an_ambient_id_is_the_id_on_the_wire():
    """The trace continues across the hop."""
    recorder = Recorder()
    client = _client(recorder)
    CorrelationContext.set("inbound-trace-abc")

    await client._call("do", {}, int)

    assert recorder.ids == ["inbound-trace-abc"], recorder.headers


async def test_both_spellings_carry_it():
    """The wire carries `X-Correlation-ID` and `correlation_id`, and they agree."""
    recorder = Recorder()
    client = _client(recorder)
    CorrelationContext.set("inbound-trace-abc")

    await client._call("do", {}, int)

    sent = recorder.headers[0]
    assert sent["X-Correlation-ID"] == "inbound-trace-abc"
    assert sent["correlation_id"] == "inbound-trace-abc"


async def test_every_request_in_one_scope_shares_the_id():
    """Including the describe: one inbound request is one trace, not two."""
    recorder = Recorder()
    client = ServiceClient(service="svc")
    client.SIGNATURES = {"do": "sha256:x"}
    client._nc = AsyncMock()
    client._request = recorder.request  # type: ignore[method-assign]
    CorrelationContext.set("inbound-trace-abc")

    await client._call("do", {}, int)

    assert any(s.endswith("describe") for s in recorder.subjects), recorder.subjects
    assert set(recorder.ids) == {"inbound-trace-abc"}, list(
        zip(recorder.subjects, recorder.ids, strict=False)
    )


async def test_the_client_and_call_rpc_agree_on_the_id():
    """The property the split broke: two ways to call a peer, one trace.

    `call_rpc` resolves `CorrelationContext.get() or get_or_create_id()`. A
    generated client must reach the same answer inside the same scope, or a
    service that switches between them silently splits its trace.
    """
    recorder = Recorder()
    client = _client(recorder)
    CorrelationContext.set("inbound-trace-abc")

    await client._call("do", {}, int)
    from_call_rpc = CorrelationContext.get() or CorrelationContext.get_or_create_id()

    assert recorder.ids[0] == from_call_rpc


# --- what must not change ---------------------------------------------------


async def test_CONTROL_with_no_ambient_id_each_call_gets_a_fresh_one():
    """The existing promise: unrelated requests do not share an identifier."""
    recorder = Recorder()
    client = _client(recorder)
    CorrelationContext.clear()

    await client._call("do", {}, int)
    await client._call("do", {}, int)

    assert len(set(recorder.ids)) == 2, recorder.ids
    assert all(len(i) == 32 for i in recorder.ids), recorder.ids


async def test_CONTROL_an_explicit_header_still_wins():
    """`publish_event` resolves an explicit id before the ambient one; so does this."""
    recorder = Recorder()
    client = _client(recorder, headers={"X-Correlation-ID": "pinned-by-the-caller"})
    CorrelationContext.set("inbound-trace-abc")

    await client._call("do", {}, int)

    assert recorder.ids == ["pinned-by-the-caller"], recorder.headers


async def test_CONTROL_the_callers_other_headers_are_untouched():
    """The id is added to the caller's headers, not substituted for them."""
    recorder = Recorder()
    client = _client(recorder, headers={"Authorization": "Bearer t"})
    CorrelationContext.set("inbound-trace-abc")

    await client._call("do", {}, int)

    assert recorder.headers[0]["Authorization"] == "Bearer t"
    assert recorder.headers[0]["X-Correlation-ID"] == "inbound-trace-abc"
