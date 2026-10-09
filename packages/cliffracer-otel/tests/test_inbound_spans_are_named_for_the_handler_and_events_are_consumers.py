"""An inbound span is named for the handler that ran, an event is a CONSUMER span, `describe` has none.

A span named `rpc orders.123.get_order` is one group per id in a tracing backend, and a saved query
on it matches one order. The handler is what the code is, so the span is `rpc get_order` whatever
subject delivered it, and the subject stays in `messaging.destination.name` and
`cliffracer.subject`. A message the service consumes without answering is a CONSUMER span, not a
SERVER one: an event handler is not a request being served. A timer fires from the service's own
clock, with no message and no peer, so its span is INTERNAL. A `describe` request is introspection,
not traffic, so it starts no span and `spans_total` does not count it.

Each test drives the real dispatcher with a message, not a hand-built context, so the `handler_name`
the span is named from is the one the dispatcher sets.
"""

import json
import re
from pathlib import Path

import pytest
from cliffracer_otel import OtelExtension
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from cliffracer import CliffracerService, ServiceConfig, async_rpc, listener, rpc
from cliffracer.core.extension import SharedDependency, WorkerContext
from cliffracer.testing import MockMessage, refuse_a_reply_with_no_subject

pytestmark = pytest.mark.unit

README = Path(__file__).resolve().parents[1] / "README.md"
EXTENSIONS_DOC = Path(__file__).resolve().parents[3] / "docs" / "extensions.md"


class Msg:
    """A request message that remembers the reply it was given."""

    def __init__(self, subject: str, data: bytes = b"{}", reply: str = "_INBOX.r") -> None:
        self.subject = subject
        self.data = data
        self.headers: dict[str, str] = {"Content-Type": "application/json"}
        self.reply = reply
        self.responded: bytes | None = None

    async def respond(self, data: bytes) -> None:
        refuse_a_reply_with_no_subject(self)
        self.responded = data


async def _service():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class Orders(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

        @rpc
        async def get_order(self, order_id: str) -> str:
            return order_id

        @async_rpc
        async def reindex(self) -> None:
            return None

        @listener("orders.*", fanout=True)
        async def on_order(self, n: int) -> None:
            return None

    svc = Orders(ServiceConfig(name="orders_svc", health_port=0))
    svc._discover_handlers()
    await svc.container._setup_extensions()
    return svc, exporter


async def _rpc(svc, subject: str) -> None:
    await svc.container._handle_rpc_request(Msg(subject, json.dumps({"order_id": "x"}).encode()))


async def _event(svc, subject: str) -> None:
    msg = MockMessage(subject, data=b'{"n": 1}', headers={"Content-Type": "application/json"})
    await svc.container.dispatcher.handle_event(msg, pattern="orders.*", raise_on_error=True)


async def test_an_rpc_span_is_named_for_the_handler_whatever_subject_delivered_it():
    svc, exporter = await _service()

    await _rpc(svc, "orders.123.get_order")
    await _rpc(svc, "orders.456.get_order")

    first, second = exporter.get_finished_spans()
    assert first.name == second.name == "rpc get_order"
    assert first.attributes["cliffracer.subject"] == "orders.123.get_order"
    assert second.attributes["messaging.destination.name"] == "orders.456.get_order"


async def test_an_async_rpc_span_is_named_for_the_handler():
    svc, exporter = await _service()

    await svc.container._handle_async_request(Msg("orders_svc.reindex", reply=""))

    (span,) = exporter.get_finished_spans()
    assert span.name == "async_rpc reindex"
    assert span.attributes["cliffracer.subject"] == "orders_svc.reindex"


async def test_an_event_span_is_named_for_the_handler_whatever_subject_delivered_it():
    svc, exporter = await _service()

    await _event(svc, "orders.123")
    await _event(svc, "orders.456")

    first, second = exporter.get_finished_spans()
    assert first.name == second.name == "event on_order"
    assert first.attributes["cliffracer.subject"] == "orders.123"
    assert second.attributes["messaging.destination.name"] == "orders.456"


async def test_an_event_delivery_is_a_consumer_span():
    svc, exporter = await _service()

    await _event(svc, "orders.123")

    (span,) = exporter.get_finished_spans()
    assert span.kind == SpanKind.CONSUMER
    assert span.attributes["messaging.operation.type"] == "process"


async def test_CONTROL_an_rpc_stays_a_server_span():
    svc, exporter = await _service()

    await _rpc(svc, "orders.123.get_order")

    (span,) = exporter.get_finished_spans()
    assert span.kind == SpanKind.SERVER


async def test_CONTROL_an_async_rpc_stays_a_server_span():
    svc, exporter = await _service()

    await svc.container._handle_async_request(Msg("orders_svc.reindex", reply=""))

    (span,) = exporter.get_finished_spans()
    assert span.kind == SpanKind.SERVER


async def test_a_describe_request_starts_no_span_and_is_not_counted():
    svc, exporter = await _service()
    msg = Msg("orders_svc.describe")

    await svc.container._handle_describe_request(msg)

    assert msg.responded, "the describe request was not answered"
    assert exporter.get_finished_spans() == ()
    health = svc.otel.health_details()
    assert (health["spans_total"], health["errors_total"], health["active_spans"]) == (0, 0, 0)


async def test_a_describe_request_before_an_rpc_leaves_only_the_rpc_traced():
    svc, exporter = await _service()

    await svc.container._handle_describe_request(Msg("orders_svc.describe"))
    await _rpc(svc, "orders_svc.get_order")

    (span,) = exporter.get_finished_spans()
    assert span.name == "rpc get_order"
    assert svc.otel.health_details()["spans_total"] == 1


async def test_a_timer_span_is_named_for_its_method_and_is_internal():
    svc, exporter = await _service()
    ctx = WorkerContext(kind="timer", subject=None, headers={}, correlation_id=None, payload={})
    ctx.data["handler_name"] = "sweep"

    async def run():
        return None

    await svc.container._run_worker(ctx, run)

    (span,) = exporter.get_finished_spans()
    assert span.name == "timer sweep"
    assert span.kind == SpanKind.INTERNAL


async def test_a_context_that_names_no_handler_is_named_for_its_kind_not_its_subject():
    svc, exporter = await _service()
    ctx = WorkerContext(
        kind="rpc", subject="orders.123.get_order", headers={}, correlation_id=None, payload={}
    )

    async def run():
        return None

    await svc.container._run_worker(ctx, run)

    (span,) = exporter.get_finished_spans()
    assert span.name == "rpc"
    assert span.attributes["cliffracer.subject"] == "orders.123.get_order"


async def test_CONTROL_an_outbound_span_is_named_as_it_was():
    svc, exporter = await _service()
    ctx = WorkerContext(
        kind="call_rpc",
        subject="billing.123.charge",
        headers={},
        correlation_id="c1",
        payload={},
    )

    async def send():
        return None

    await svc.container._run_send_hooks(ctx, send)

    (span,) = exporter.get_finished_spans()
    assert span.name == "call_rpc billing.123.charge"


def test_the_readme_states_the_span_names_the_kinds_and_that_describe_has_none():
    text = README.read_text()

    assert "`{kind} {handler}`" in text
    assert "`SpanKind.CONSUMER`" in text
    assert "`SpanKind.SERVER`" in text
    assert "`SpanKind.INTERNAL`" in text
    assert "`describe`" in text and "no span" in text


async def _every_kind_of_span():
    """One span of each inbound kind and each outbound send kind, from a service that has handled them."""
    svc, exporter = await _service()
    await _rpc(svc, "orders.123.get_order")
    await svc.container._handle_async_request(Msg("orders_svc.reindex", reply=""))
    await _event(svc, "orders.123")
    tick = WorkerContext(
        kind="timer", subject=None, headers={}, correlation_id="corr_tick", payload={}
    )
    tick.data["handler_name"] = "sweep"

    async def run():
        return None

    await svc.container._run_worker(tick, run)
    for kind in ("call_rpc", "call_async", "call_rpc_no_wait", "publish_event", "broadcast"):
        sent = WorkerContext(
            kind=kind, subject="billing.charge", headers={}, correlation_id="c1", payload={}
        )
        await svc.container._run_send_hooks(sent, run)
    spans = exporter.get_finished_spans()
    assert len(spans) == 9, [s.name for s in spans]
    return spans


async def test_no_span_carries_an_rpc_attribute():
    """The ruling sets no `rpc.*` attribute; an `rpc.method` added anywhere would otherwise pass."""
    spans = await _every_kind_of_span()

    carried = sorted({key for span in spans for key in span.attributes if key.startswith("rpc.")})
    assert carried == []


def _named_in(text: str) -> set[str]:
    """Every `messaging.*` and `cliffracer.*` attribute name written in backticks in `text`."""
    return set(re.findall(r"`((?:messaging|cliffracer)\.[a-z_.]+)`", text))


def _attributes_section() -> str:
    text = README.read_text()
    (bullet,) = re.findall(r"^- \*\*Attributes\*\*:.*$", text, re.M)
    return bullet


async def test_the_readmes_attribute_list_is_exactly_what_the_extension_sets():
    spans = await _every_kind_of_span()

    set_by_the_extension = {key for span in spans for key in span.attributes}
    assert _named_in(_attributes_section()) == set_by_the_extension


async def test_every_attribute_the_readme_and_the_extensions_guide_name_is_one_a_span_carries():
    spans = await _every_kind_of_span()
    set_by_the_extension = {key for span in spans for key in span.attributes}
    guide = EXTENSIONS_DOC.read_text()
    section = guide[guide.index("## OpenTelemetry Distributed Tracing") :]
    section = section[: section.index("\n## ", 1)]

    assert _named_in(README.read_text()) <= set_by_the_extension
    assert _named_in(section) <= set_by_the_extension


async def test_a_failed_describe_request_starts_no_span_and_is_not_counted_as_an_error(monkeypatch):
    def exploded(*args, **kwargs):
        raise RuntimeError("describe exploded")

    monkeypatch.setattr("cliffracer.introspect.describe", exploded)
    svc, exporter = await _service()

    await svc.container._handle_describe_request(Msg("orders_svc.describe"))

    assert exporter.get_finished_spans() == ()
    health = svc.otel.health_details()
    assert (health["spans_total"], health["errors_total"], health["active_spans"]) == (0, 0, 0)


def test_the_readme_says_the_counters_do_not_count_a_describe_request_failed_or_not():
    text = " ".join(README.read_text().split())

    assert "`describe` request starts no span" in text
    assert "neither `spans_total` nor `errors_total` counts it" in text
    assert "every dispatch the extension started a span for" in text
