"""Every before and after in docs/upgrading.md is run against the code, and does what its entry says.

The snippets are read out of the page, so a snippet edited there is the one that runs. Each entry has
one row below: how its "before" is held to fail (an exception, or an assertion the old behaviour
satisfied and the current code does not), and what its "after" does. A page section with no row, or
a row with no section, fails the coverage test, and the swap control runs every "after" through its
row's "before" check and the reverse, so a row that accepts either is reported.

Nothing here dials a broker. A service's startup checks are run through the step `start()` runs
before it connects; the calls that need a transport run over a stand-in that records what is sent.
"""

import asyncio
import dataclasses
import importlib
import io
import json
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import nats.errors
import nats.js.errors
import pytest
from cliffracer_auth import AuthConfig, SimpleAuthService
from cliffracer_cron import DistributedCronTimer
from cliffracer_cyanide import CyanideConfig, CyanideExtension
from cliffracer_kv import BucketConfigError, KvExtension, ModelDoesNotReadBackError
from cliffracer_resilience import (
    InMemoryRateLimiter,
    RateLimitConfig,
    RateLimitExceeded,
    ResilienceExtension,
    rate_limit,
)
from loguru import logger
from nats.aio.msg import Msg
from nats.errors import NotJSMessageError
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from cliffracer import (
    CliffracerService,
    Extension,
    RejectMessage,
    ServiceConfig,
    listener,
    rpc,
    timer,
)
from cliffracer.cli.config import ConfigError
from cliffracer.cli.main import build_orchestrator
from cliffracer.client import ServiceClient
from cliffracer.core import typed_rpc, validation
from cliffracer.core.dispatch.dlq import DeadLetterPublisher
from cliffracer.core.exceptions import (
    ClientOutOfDateError,
    ConfigurationError,
    RpcClientError,
    RpcConnectionError,
    RpcError,
    RpcNoRespondersError,
    RpcRefusedError,
    RpcServerError,
    RpcTimeoutError,
    RpcValidationError,
    ServiceLifecycleError,
    raise_for_error_envelope,
)
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec, ensure_streams
from cliffracer.core.typed_rpc import UntypedHandler, return_type_ref, type_ref
from cliffracer.generate_client import emit
from cliffracer.introspect import Description, _signature_hash, describe
from cliffracer.runners.contracts import ActivationConflict
from cliffracer.runners.templates import ServiceTemplate, TemplateCatalog
from cliffracer.testing import MockJetStreamMetadata, MockMessage, ServiceTestHarness
from tests.phase_stubs import ServicePhases

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
UPGRADING = REPO / "docs" / "upgrading.md"

Source = str
Check = Callable[[Source], None]


class _Item(BaseModel):
    """A model the consumer in the page imports by its module and name."""

    sku: str


def sections(text: str | None = None) -> dict[str, dict[str, Source]]:
    """Entry title -> its "before" (```text) and "after" (```python) snippets."""
    page = UPGRADING.read_text() if text is None else text
    found: dict[str, dict[str, Source]] = {}
    for part in re.split(r"^### ", page, flags=re.M)[1:]:
        title, _, body = part.partition("\n")
        body = re.split(r"^## ", body, flags=re.M)[0]
        before = re.findall(r"^```text\n(.*?)^```", body, re.S | re.M)
        after = re.findall(r"^```python\n(.*?)^```", body, re.S | re.M)
        assert len(before) == 1 and len(after) == 1, (
            f"{title!r} has {len(before)} before and {len(after)} after snippets; it needs one each"
        )
        found[title] = {"before": before[0], "after": after[0]}
    return found


def run(source: Source) -> dict[str, Any]:
    namespace: dict[str, Any] = {"__name__": "upgrading_snippet"}
    exec(compile(source, "<docs/upgrading.md>", "exec"), namespace)
    return namespace


def refused(exc: type[BaseException], match: str) -> Check:
    """The snippet raises `exc` whose text matches, as it runs."""

    def check(source: Source) -> None:
        with pytest.raises(exc, match=match):
            run(source)

    return check


def clean(source: Source) -> None:
    run(source)


def refused_at_startup(cls: str, exc: type[BaseException], match: str) -> Check:
    """The snippet defines the service class `cls`, and the step `start()` runs before it connects raises."""

    def check(source: Source) -> None:
        service = run(source)[cls]()
        with pytest.raises(exc, match=match):
            service.container._discover_for_startup()

    return check


def starts(cls: str) -> Check:
    def check(source: Source) -> None:
        run(source)[cls]().container._discover_for_startup()

    return check


def _spans_a_service_made() -> list[Any]:
    """The finished spans of a service that handled an RPC, an event, a timer tick and a describe."""
    from cliffracer_otel import OtelExtension
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from cliffracer import listener, rpc
    from cliffracer.core.extension import SharedDependency, WorkerContext
    from cliffracer.testing import MockMessage, refuse_a_reply_with_no_subject

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class Orders(CliffracerService):
        otel = OtelExtension(tracer_provider=SharedDependency(provider))

        @rpc
        async def get_order(self, order_id: str) -> str:
            return order_id

        @listener("orders.*", fanout=True)
        async def on_order(self, n: int) -> None:
            return None

    class Request(MockMessage):
        reply = "_INBOX.r"

        async def respond(self, data: bytes) -> None:
            refuse_a_reply_with_no_subject(self)

    async def drive() -> None:
        svc = Orders(ServiceConfig(name="orders_svc", health_port=0))
        svc._discover_handlers()
        await svc.container._setup_extensions()
        headers = {"Content-Type": "application/json"}
        await svc.container._handle_rpc_request(
            Request("orders.123.get_order", data=b'{"order_id": "x"}', headers=headers)
        )
        await svc.container.dispatcher.handle_event(
            MockMessage("orders.123", data=b'{"n": 1}', headers=headers),
            pattern="orders.*",
            raise_on_error=True,
        )
        await svc.container._handle_describe_request(Request("orders_svc.describe", data=b"{}"))
        tick = WorkerContext(
            kind="timer", subject=None, headers={}, correlation_id=None, payload={}
        )
        tick.data["handler_name"] = "sweep"

        async def run() -> None:
            return None

        await svc.container._run_worker(tick, run)

    asyncio.run(drive())
    return list(exporter.get_finished_spans())


def _run_over_spans(source: Source, refused_message: str | None = None) -> None:
    """Call the snippet's `check` with the spans a real dispatch produced.

    With `refused_message` the snippet is expected to fail an assertion carrying it.
    """
    spans = _spans_a_service_made()
    assert len(spans) == 3, (
        f"expected an rpc, an event and a timer span, got {[s.name for s in spans]}"
    )
    check = run(source)["check"]
    if refused_message is None:
        check(spans)
        return
    with pytest.raises(AssertionError, match=refused_message):
        check(spans)


# ---- the entries ------------------------------------------------------------------------------


def _two_nats_credentials_after(source: Source) -> None:
    config = run(source)["config"]
    assert config.nats_auth_kwargs() == {"token": "t0ken"}
    assert config.nats_user is None


def _durable_refused_at_startup(source: Source) -> None:
    """The step `start()` runs before it connects: the refusal names the subject, durable and handler."""
    service = run(source)["Billing"]()
    with pytest.raises(StreamDeclarationError, match="billing") as caught:
        service.container._discover_for_startup()
    for named in ("'orders.created'", "'billing'", "'on_created'"):
        assert named in str(caught.value), (named, str(caught.value))


def _a_zero_drift_and_backoff_are_allowed(source: Source) -> None:
    run(source)

    @timer(interval=1, max_drift=0, error_backoff=0)
    async def tick(self) -> None: ...


def _stream_subjects(source: Source) -> None:
    assert run(source)["orders"].subjects == [
        "shop.events.orders.*",
        "wholesale.events.orders.*",
    ]


def _call_rpc_service(*, reply: dict[str, Any] | None = None, lost: bool = False):
    service = CliffracerService(ServiceConfig(name="caller"))
    service.nc = AsyncMock()
    if lost:
        service.nc.request.side_effect = nats.errors.ConnectionClosedError()
    else:
        answer = AsyncMock()
        answer.data = json.dumps(reply).encode()
        service.nc.request.return_value = answer
    return service


DETAILS = [{"loc": ["sku"], "msg": "field required"}]
VALIDATION_REPLY = {
    "success": False,
    "code": "validation_failed",
    "error": "validation failed",
    "details": DETAILS,
}


def _call_rpc_before(source: Source) -> None:
    reserve = run(source)["reserve"]
    # The text test and the nats `except` both miss, so the typed error propagates.
    with pytest.raises(RpcValidationError):
        asyncio.run(reserve(_call_rpc_service(reply=VALIDATION_REPLY), "a1"))
    with pytest.raises(RpcConnectionError) as caught:
        asyncio.run(reserve(_call_rpc_service(lost=True), "a1"))
    assert isinstance(caught.value, RpcError)
    assert isinstance(caught.value.__cause__, nats.errors.ConnectionClosedError)


def _call_rpc_after(source: Source) -> None:
    reserve = run(source)["reserve"]
    assert asyncio.run(reserve(_call_rpc_service(reply=VALIDATION_REPLY), "a1")) == DETAILS
    assert asyncio.run(reserve(_call_rpc_service(lost=True), "a1")) == "retry"


def _oversized_service() -> CliffracerService:
    service = CliffracerService(ServiceConfig(name="caller"))
    service.nc = AsyncMock()
    service.nc.request.side_effect = nats.errors.MaxPayloadError()
    return service


def _send_error_before(source: Source) -> None:
    charge = run(source)["charge"]
    # The nats `except` misses, so the mapped error propagates, with the nats error as its cause.
    with pytest.raises(RpcClientError) as caught:
        asyncio.run(charge(_oversized_service(), 1))
    assert isinstance(caught.value.__cause__, nats.errors.MaxPayloadError)


def _send_error_after(source: Source) -> None:
    assert asyncio.run(run(source)["charge"](_oversized_service(), 1)) == "too large"


def _no_responders_service() -> CliffracerService:
    service = CliffracerService(ServiceConfig(name="caller"))
    service.nc = AsyncMock()
    service.nc.request.side_effect = nats.errors.NoRespondersError()
    return service


def _no_responders_before(source: Source) -> None:
    notify = run(source)["notify"]
    with pytest.raises(RpcNoRespondersError) as caught:
        asyncio.run(notify(_no_responders_service(), "a1"))
    assert isinstance(caught.value, RpcError)
    assert isinstance(caught.value.__cause__, nats.errors.NoRespondersError)


def _no_responders_after(source: Source) -> None:
    assert asyncio.run(run(source)["notify"](_no_responders_service(), "a1")) == "gone"


def _without_msgpack():
    return patch.object(validation, "msgpack", None)


def _msgpack_before(source: Source) -> None:
    ingest = run(source)["Ingest"]()
    with (
        _without_msgpack(),
        pytest.raises(ConfigurationError, match="'msgpack' package is not installed"),
    ):
        ingest.container._discover_for_startup()


def _msgpack_after(source: Source) -> None:
    ingest = run(source)["Ingest"]()
    with _without_msgpack():
        ingest.container._discover_for_startup()


def _brokerless(worker_class: type) -> CliffracerService:
    """The snippet's service with its broker steps stood in for, so `start()` runs without one."""

    class Brokerless(ServicePhases, worker_class):  # type: ignore[misc, valid-type]
        async def connect(self) -> None: ...

        async def disconnect(self) -> None: ...

        async def _setup_subscriptions(self) -> None: ...

    return Brokerless(ServiceConfig(name="worker", health_port=0))


def _stopped_while_starting_before(source: Source) -> None:
    namespace = run(source)
    with pytest.raises(ServiceLifecycleError, match="was stopped while it was starting"):
        asyncio.run(namespace["serve"](_brokerless(namespace["Worker"])))


def _stopped_while_starting_after(source: Source) -> None:
    namespace = run(source)
    assert (
        asyncio.run(namespace["serve"](_brokerless(namespace["Worker"])))
        == "stopped while starting"
    )


def _python_type_after(source: Source) -> None:
    annotation = run(source)["annotation"]
    assert annotation(type_ref(int)) is int
    assert annotation(type_ref(list[int])) == list[int]
    assert annotation(type_ref(dict[str, float])) == dict[str, float]
    assert annotation(type_ref(int | None)) == int | None
    constrained = TypeAdapter(annotation(type_ref(Annotated[int, Field(ge=1)])))
    assert constrained.validate_python(1) == 1
    with pytest.raises(ValidationError):
        constrained.validate_python(0)
    assert annotation(type_ref(_Item)) is _Item
    literal = annotation({"kind": "literal", "values": ["a", "b"]})
    assert TypeAdapter(literal).validate_python("a") == "a"
    with pytest.raises(ValidationError):
        TypeAdapter(literal).validate_python("c")
    assert annotation(return_type_ref(AsyncIterator[int])) == AsyncIterator[int]


def _secret_after(source: Source) -> None:
    namespace = run(source)
    assert namespace["signing_key"] == KEY.encode()
    assert KEY not in repr(namespace["config"])
    assert KEY not in namespace["config"].model_dump_json()


def _service_config_secret_after(source: Source) -> None:
    namespace = run(source)
    assert namespace["password"] == "S3CRET"
    assert "s3cret" not in repr(namespace["config"])
    assert "s3cret" not in namespace["config"].model_dump_json()


SECRET = "the-signing-key-" + "k" * 24
KEY = "0123456789abcdef0123456789abcdef"


def _a_chain_that_began(service: SimpleAuthService, hours: float) -> str:
    return service._mint_token(
        service._users["alice"]["user"], original_iat=time.time() - hours * 3600
    )


def _auth_service(config: AuthConfig) -> SimpleAuthService:
    service = SimpleAuthService(config)
    service.create_user("alice", "alice@example.com", "a-long-enough-password")
    return service


def _refresh_before(source: Source) -> None:
    refused(AssertionError, "no cap")(source)
    # What the entry says in its place: the default config refuses a chain 31 days old.
    default = _auth_service(AuthConfig(secret_key=SECRET))
    assert default.refresh_token(_a_chain_that_began(default, 31 * 24)) is None


def _refresh_after(source: Source) -> None:
    assert run(source)["config"].refresh_max_lifetime_hours is None
    service = _auth_service(AuthConfig(secret_key=SECRET, refresh_max_lifetime_hours=None))
    assert service.refresh_token(_a_chain_that_began(service, 24 * 365 * 10)) is not None


def _verifier(namespace: dict[str, Any]) -> SimpleAuthService:
    """A service verifying with the snippet's key, at the default `token_expiry_hours` of 24."""
    return SimpleAuthService(AuthConfig(secret_key=namespace["key"]))


def _longer_token_before(source: Source) -> None:
    namespace = run(source)
    assert _verifier(namespace).validate_token(namespace["token"]) is None


def _longer_token_after(source: Source) -> None:
    namespace = run(source)
    assert _verifier(namespace).validate_token(namespace["token"]) is not None


def _bucket(*, existing_value: bytes | None = None):
    extension = KvExtension()
    bucket = AsyncMock()
    extension.get_bucket = AsyncMock(return_value=bucket)  # type: ignore[method-assign]
    extension.get = AsyncMock(return_value=existing_value)  # type: ignore[method-assign]
    return extension, bucket


def _kv_put_before(source: Source) -> None:
    extension, bucket = _bucket()
    with pytest.raises(TypeError, match="cannot store"):
        asyncio.run(run(source)["save"](extension))
    bucket.put.assert_not_awaited()


def _kv_save_extension() -> tuple[KvExtension, AsyncMock, AsyncMock]:
    """A `KvExtension` whose KV bucket and object store are stand-ins that record the writes."""
    extension, bucket = _bucket()
    store = AsyncMock()
    extension.get_object_store = AsyncMock(return_value=store)  # type: ignore[method-assign]
    return extension, bucket, store


@dataclass
class _Report:
    id: str


def _the_other_writes_store_json_or_raise_the_same_way() -> None:
    """The second paragraph of "A KV write stores JSON or raises": `put_object`, `NaN`, iterators, files."""
    extension, bucket, store = _kv_save_extension()

    # `put_object` stores what `put` stores, where nats-py raised for a bare dataclass.
    asyncio.run(extension.put_object("reports", "r1", _Report(id="r1")))
    store.put.assert_awaited_once_with("r1", b'{"id": "r1"}', meta=None)
    # And a value with no JSON form raises from it as from `put`, naming the type.
    for write in (
        lambda: extension.put_object("reports", "r2", object()),
        lambda: extension.put("jobs", "j1", object()),
    ):
        with pytest.raises(TypeError, match="cannot store a object"):
            asyncio.run(write())

    # Not JSON: NaN and infinity, and what is read by being consumed, alone or nested.
    for value in (
        math.nan,
        math.inf,
        {"n": math.nan},
        iter([1, 2]),
        {"rows": iter([1])},
        [io.StringIO("x")],
    ):
        for write in (
            lambda value=value: extension.put("jobs", "j1", value),
            lambda value=value: extension.create("jobs", "j1", value),
            lambda value=value: extension.put_object("reports", "r3", value),
        ):
            with pytest.raises(TypeError, match="cannot store a"):
                asyncio.run(write())
    bucket.put.assert_not_awaited()
    bucket.create.assert_not_awaited()
    assert store.put.await_count == 1, "only the dataclass was written"

    # A file given to `put_object` on its own is streamed, not refused.
    stream = io.BytesIO(b"bytes")
    asyncio.run(extension.put_object("reports", "r4", stream))
    assert store.put.await_args.args[1] is stream


def _kv_put_after(source: Source) -> None:
    extension, bucket = _bucket()
    bucket.put.return_value = 7
    assert asyncio.run(run(source)["save"](extension)) == 7
    bucket.put.assert_awaited_once_with("j1", b'{"id": "j1"}')
    _the_other_writes_store_json_or_raise_the_same_way()


def _kv_delete_before(source: Source) -> None:
    extension, bucket = _bucket()
    with pytest.raises(AssertionError, match="says whether"):
        asyncio.run(run(source)["forget"](extension, "k"))
    bucket.delete.assert_awaited_once()


def _kv_delete_after(source: Source) -> None:
    forget = run(source)["forget"]
    extension, bucket = _bucket(existing_value=b"x")
    assert asyncio.run(forget(extension, "k")) is True
    bucket.delete.assert_awaited_once_with("k", last=None)
    extension, bucket = _bucket(existing_value=None)
    assert asyncio.run(forget(extension, "k")) is False
    bucket.delete.assert_awaited_once()


def _declared_bucket_history(extension: KvExtension, config: ServiceConfig) -> int | None:
    """The `history` of the bucket `sessions` as the service start provisions it, None if it is not.

    The service start is `setup()` then `start()`, which creates every declared bucket; the broker is a
    stand-in that has no bucket, so what reaches `create_key_value` is what was declared.
    """
    js = AsyncMock()
    js.key_value.side_effect = nats.js.errors.BucketNotFoundError
    extension._explicit_js = js
    asyncio.run(extension.setup(SimpleNamespace(service_config=config)))  # type: ignore[arg-type]
    asyncio.run(extension.start())
    created = [call.kwargs for call in js.create_key_value.await_args_list]
    assert len(created) <= 1, created
    return created[0].get("history", 1) if created else None


def _kv_declared_twice_before(source: Source) -> None:
    extension = run(source)["extension"]
    with pytest.raises(BucketConfigError, match="'sessions' is declared twice"):
        extension._declare()


def _kv_declared_once_after(source: Source) -> None:
    extension = run(source)["extension"]
    extension._declare()
    assert extension._bucket_configs["sessions"].history == 5


def _kv_buckets_before(source: Source) -> None:
    config = run(source)["OrdersConfig"](name="orders")
    assert config.kv_buckets, "the config names the bucket the entry says it names"
    assert _declared_bucket_history(KvExtension(), config) is None, (
        "the bucket named on the config is provisioned"
    )


def _kv_buckets_after(source: Source) -> None:
    extension = run(source)["extension"]
    assert _declared_bucket_history(extension, ServiceConfig(name="orders")) == 5


def _kv_model_before(source: Source) -> None:
    extension, bucket = _bucket()
    with pytest.raises(ModelDoesNotReadBackError, match="Job"):
        asyncio.run(run(source)["save"](extension))
    bucket.put.assert_not_awaited()


def _kv_model_after(source: Source) -> None:
    namespace = run(source)
    extension, bucket = _bucket()
    bucket.put.return_value = 4
    assert asyncio.run(namespace["save"](extension)) == 4
    stored = bucket.put.await_args.args[1]
    assert stored == b'{"state":"DONE"}'
    assert namespace["Job"].model_validate_json(stored) == namespace["Job"](state="done")


def _breaker_after(source: Source) -> None:
    assert run(source)["config"].monitored_exceptions == (ValueError,)


def _rate_limiter_before(source: Source) -> None:
    refused(AssertionError, "not in the bucket name")(source)


def _rate_limiter_after(source: Source) -> None:
    keep = run(source)["keep_the_existing_counters"]
    js = AsyncMock()
    limiter = asyncio.run(keep(js))
    asyncio.run(limiter.init_kv(js=js))
    # The bucket handed over is the one it counts in: nothing is created under another name.
    js.key_value.assert_awaited_once_with("rate_limits")
    js.create_key_value.assert_not_awaited()


def _percentile_after(source: Source) -> None:
    run(source)


def _batch(source: Source) -> None:
    asyncio.run(run(source)["main"]())


def _batch_before(source: Source) -> None:
    with pytest.raises(AssertionError, match="own element"):
        _batch(source)


def _bucket_after(source: Source) -> None:
    sessions = run(source)["sessions"]
    assert sessions.history == 1


@asynccontextmanager
async def _serving(service_cls: type[CliffracerService], name: str):
    """`service_cls` started in process, and a caller whose requests it answers.

    Yields `(caller, harness, sent)`. `caller.call_rpc(...)` and `caller.nc.request(...)` go through
    the service's own RPC dispatch and read the reply it wrote, with no broker; `sent` holds each
    request's message after the dispatch, so its `responded_data` and `response_headers` are what the
    service published.
    """
    config = ServiceConfig(name=name, health_port=0)
    async with ServiceTestHarness(service_cls, config=config) as harness:
        sent: list[MockMessage] = []

        async def request(subject, data, timeout=None, headers=None):
            message = MockMessage(subject, data=data, headers=headers or {}, reply="_INBOX.r")
            if subject.endswith(".describe"):
                await harness.container._handle_describe_request(message)
            else:
                await harness.container._handle_rpc_request(message)
            sent.append(message)
            return SimpleNamespace(
                data=message.responded_data, headers=dict(message.response_headers)
            )

        caller = CliffracerService(ServiceConfig(name="caller", health_port=0))
        caller.nc = AsyncMock()
        caller.nc.request.side_effect = request
        yield caller, harness, sent


class _Catalog(CliffracerService):
    resilience = ResilienceExtension()

    @rpc
    @rate_limit(calls=5, window=60.0, key="client", key_source="payload")
    async def search(self, query: str, client: str = "") -> str:
        return query


class _Limited(CliffracerService):
    resilience = ResilienceExtension(default_calls=2, default_window=60.0)

    @rpc
    async def ok(self) -> int:
        return 1


async def _a_default_limit_is_for_callers() -> None:
    async with _serving(_Limited, "limited") as (caller, _, sent):
        for _ in range(4):
            reply = await caller.nc.request("limited.describe", b"{}", headers={})
            assert "error" not in json.loads(reply.data), reply.data
        assert await caller.call_rpc("limited", "ok") == 1
        assert await caller.call_rpc("limited", "ok") == 1
        with pytest.raises(RpcRefusedError, match="rate limit exceeded"):
            await caller.call_rpc("limited", "ok")


def _missing_key_before(source: Source) -> None:
    search = run(source)["search"]

    async def drive() -> None:
        async with _serving(_Catalog, "catalog") as (caller, _, sent):
            with pytest.raises(
                RpcRefusedError, match="rate-limit key 'client' is missing"
            ) as caught:
                await search(caller, "x")
            # A refusal, which no `except RpcServerError` catches; not an `internal` reply.
            assert not isinstance(caught.value, RpcServerError)
            reply = json.loads(sent[-1].responded_data)
            assert reply["code"] == "refused", reply

    asyncio.run(drive())


def _missing_key_after(source: Source) -> None:
    search = run(source)["search"]

    async def drive() -> None:
        async with _serving(_Catalog, "catalog") as (caller, _, sent):
            assert await search(caller, "x") == "x"
            with pytest.raises(RpcRefusedError, match="rate-limit key 'client' is missing"):
                await caller.call_rpc("catalog", "search", query="x")
            assert json.loads(sent[-1].responded_data)["code"] == "refused"
            # A key left out spent no permit; the sixth call with it is the limit's own refusal.
            answers = [await search(caller, "x") for _ in range(5)]
            assert answers[:-1] == ["x"] * 4
            assert answers[-1] == "rate limit exceeded"
        await _a_default_limit_is_for_callers()

    asyncio.run(drive())


BILLING = ({"amount": 5}, {"amount": 5000}, {"amount": "lots"})


async def _billing_replies(service_cls: type[CliffracerService]) -> list[dict[str, Any]]:
    config = ServiceConfig(name="billing", health_port=0)
    async with ServiceTestHarness(service_cls, config=config) as harness:
        return [(await harness.rpc("charge", payload=payload)).data for payload in BILLING]


class _Deny(Extension):
    """A gate that refuses everything, as authentication does for an unauthenticated caller."""

    def __init__(self) -> None:
        self.ran = 0

    async def worker_setup(self, ctx) -> None:
        self.ran += 1
        raise RejectMessage("unauthenticated")


class _Observer(Extension):
    """Admits everything and records what a declared extension can read at each hook."""

    def __init__(self) -> None:
        self.setup_saw_arguments: list[bool] = []
        self.result_saw: list[Any] = []

    async def worker_setup(self, ctx) -> None:
        self.setup_saw_arguments.append("validated_kwargs" in ctx.data)

    async def worker_result(self, ctx, result, exc) -> None:
        self.result_saw.append(ctx.data.get("validated_kwargs"))


def _the_order_the_entry_describes() -> None:
    class Locked(CliffracerService):
        deny = _Deny()

        @rpc
        async def charge(self, amount: int) -> int:
            return amount

    class Watched(CliffracerService):
        watch = _Observer()

        @rpc
        async def charge(self, amount: int) -> int:
            return amount

    def extension(harness: ServiceTestHarness, name: str) -> Any:
        return next(e for e in harness.container.extensions if e.name == name)

    async def drive() -> None:
        config = ServiceConfig(name="billing", health_port=0)
        async with ServiceTestHarness(Locked, config=config) as harness:
            reply = (await harness.rpc("charge", payload={"amount": "lots"})).data
            assert reply["code"] == "refused" and reply["error"] == "refused: unauthenticated"
            assert "details" not in reply, reply
            with pytest.raises(RpcRefusedError):
                raise_for_error_envelope(reply, "billing.rpc.charge")
            ran = extension(harness, "deny").ran
            garbled = MockMessage(
                "billing.rpc.charge",
                data=b"not json",
                headers={"Content-Type": "application/json"},
                reply="_INBOX.r",
            )
            await harness.container._handle_rpc_request(garbled)
            body = json.loads(garbled.responded_data)
            assert body["code"] == "validation_failed", body
            assert extension(harness, "deny").ran == ran, "a body that cannot be decoded met a gate"
        async with ServiceTestHarness(Watched, config=config) as harness:
            reply = (await harness.rpc("charge", payload={"amount": "5"})).data
            assert reply["result"] == 5, reply
            watcher = extension(harness, "watch")
            assert watcher.setup_saw_arguments == [False]
            assert watcher.result_saw == [{"amount": 5}]

    asyncio.run(drive())


def _validation_order_before(source: Source) -> None:
    replies = asyncio.run(_billing_replies(run(source)["Billing"]))

    # The gate read `validated_kwargs` in `worker_setup`: it raises, and fails closed.
    assert [reply["code"] for reply in replies] == ["internal"] * 3, replies
    assert not any(reply["success"] for reply in replies)


def _validation_order_after(source: Source) -> None:
    replies = asyncio.run(_billing_replies(run(source)["Billing"]))

    assert replies[0]["success"] is True and replies[0]["result"] == 5, replies[0]
    assert replies[1]["code"] == "refused" and replies[1]["error"] == "refused: over the cap"
    assert replies[2]["code"] == "validation_failed", replies[2]
    _the_order_the_entry_describes()


class _Echo(CliffracerService):
    @rpc
    async def ok(self) -> int:
        return 1


REQUEST_HEADERS = {"X-Tenant": "acme", "Authorization": "Bearer t"}


def _headers_before(source: Source) -> None:
    tenant_of = run(source)["tenant_of"]

    async def drive() -> None:
        async with _serving(_Echo, "svc") as (caller, _, _sent):
            with pytest.raises(KeyError, match="X-Tenant"):
                await tenant_of(caller.nc, "svc.rpc.ok")

    asyncio.run(drive())


def _headers_after(source: Source) -> None:
    tenant_of = run(source)["tenant_of"]

    async def drive() -> None:
        async with _serving(_Echo, "svc") as (caller, _, sent):
            tenant, correlation_id = await tenant_of(caller.nc, "svc.rpc.ok")
            assert tenant == "acme"
            assert dict(sent[-1].response_headers) == {
                "Content-Type": "application/json",
                "X-Correlation-ID": correlation_id,
            }
            assert json.loads(sent[-1].responded_data)["correlation_id"] == correlation_id
            described = await caller.nc.request("svc.describe", b"{}", headers=REQUEST_HEADERS)
            assert set(described.headers) <= {"Content-Type", "X-Correlation-ID"}, described.headers

    asyncio.run(drive())


def _alias_before(source: Source) -> None:
    cart = run(source)["Cart"]
    config = ServiceConfig(name="cart", health_port=0)
    with pytest.raises(UntypedHandler, match="Cart.take: parameter 'item' declares alias='itemId'"):
        cart(config).container._discover_for_startup()
    with pytest.raises(UntypedHandler, match="Cart.take: parameter 'item' declares"):
        describe(cart, service="cart", version="1", config=config)


def _alias_after(source: Source) -> None:
    cart = run(source)["Cart"]
    cart(ServiceConfig(name="cart", health_port=0)).container._discover_for_startup()

    async def drive() -> None:
        async with ServiceTestHarness(cart, config=ServiceConfig(name="cart", health_port=0)) as h:
            assert (await h.rpc("take", payload={"item": 3})).data["result"] == 3

    asyncio.run(drive())
    described = describe(
        cart, service="cart", version="1", config=ServiceConfig(name="cart", health_port=0)
    )
    assert "item" in json.dumps(described.to_dict())

    class Orders(CliffracerService):
        @listener("orders.created", fanout=True)
        async def on_created(self, item: Annotated[int, Field(alias="itemId")]) -> None: ...

    with pytest.raises(UntypedHandler, match="on_created: parameter 'item' declares alias"):
        Orders(ServiceConfig(name="orders", health_port=0)).container._discover_for_startup()


def _a_declared_limit(source: Source) -> Any:
    return run(source)["create_order"]


BAD_CALLS = [0, -1, True, 2.0, 2.5, "3", None]
BAD_WINDOWS = [0, -1, True, "60", None, math.nan, math.inf]


def _every_refusal_the_entry_lists() -> None:
    """Each declaration the entry names raises where it is made, in each of the three places.

    `None` given to the extension's `default_calls` or `default_window` is the one left out, which is
    the both-or-neither refusal checked below, so it is not listed there as a bad figure.
    """
    for calls in BAD_CALLS:
        declarations = [
            lambda calls=calls: rate_limit(calls, 60.0),
            lambda calls=calls: RateLimitConfig(calls, 60.0),
        ]
        if calls is not None:
            declarations.append(
                lambda calls=calls: ResilienceExtension(default_calls=calls, default_window=60.0)
            )
        for declare in declarations:
            with pytest.raises(
                ConfigurationError, match="calls must be a whole number of at least 1"
            ):
                declare()
    for window in BAD_WINDOWS:
        declarations = [
            lambda window=window: rate_limit(3, window),
            lambda window=window: RateLimitConfig(3, window),
        ]
        if window is not None:
            declarations.append(
                lambda window=window: ResilienceExtension(default_calls=3, default_window=window)
            )
        for declare in declarations:
            with pytest.raises(
                ConfigurationError, match="window must be a finite number of seconds"
            ):
                declare()
    for given in ({"default_calls": 3}, {"default_window": 60.0}):
        with pytest.raises(ConfigurationError, match="together"):
            ResilienceExtension(**given)
    # What the entry does not name is still accepted.
    for calls, window in [(1, 1), (3, 0.5), (10, 60.0)]:
        rate_limit(calls, window)
        RateLimitConfig(calls, window)
        ResilienceExtension(default_calls=calls, default_window=window)
    ResilienceExtension()


def _a_limit_refused_where_declared_before(source: Source) -> None:
    refused(ConfigurationError, "window must be a finite number of seconds greater than 0")(source)


def _a_limit_refused_where_declared_after(source: Source) -> None:
    create_order = _a_declared_limit(source)

    async def drive() -> None:
        for _ in range(10):
            await create_order()
        with pytest.raises(RateLimitExceeded):
            await create_order()

    asyncio.run(drive())
    _every_refusal_the_entry_lists()


class _Recording(InMemoryRateLimiter):
    """An in-memory limiter that keeps the keys it was asked to count."""

    def __init__(self) -> None:
        super().__init__()
        self.keys: list[str] = []

    async def acquire(self, key: str, calls: int, window: float) -> bool:
        self.keys.append(key)
        return await super().acquire(key, calls, window)


def _counting_service(limiter: InMemoryRateLimiter) -> type[CliffracerService]:
    class Catalog(CliffracerService):
        resilience = ResilienceExtension(limiter=limiter)

        @rpc
        @rate_limit(calls=2, window=60.0, key="client", key_source="payload")
        async def search(self, client: str) -> str:
            return "found"

        @rpc
        @rate_limit(calls=2, window=60.0, key="client", key_source="payload")
        async def export(self, client: str) -> str:
            return "exported"

    return Catalog


async def _spend(
    harness: ServiceTestHarness, method: str = "search", caller: str = "alice"
) -> dict:
    return (await harness.rpc(method, payload={"client": caller})).data


async def _spent_by_alice(harness: ServiceTestHarness) -> None:
    """Two calls fill the limit, and the third is refused."""
    for _ in range(2):
        assert (await _spend(harness))["success"] is True
    assert (await _spend(harness))["code"] == "refused"


def _counted_under_the_service_and_the_handler() -> None:
    async def drive() -> None:
        # Two handlers keyed on one header count a caller each on their own.
        shared = _Recording()
        config = ServiceConfig(name="catalog", health_port=0)
        async with ServiceTestHarness(_counting_service(shared), config=config) as harness:
            await _spent_by_alice(harness)
            assert (await _spend(harness, "export"))["success"] is True
        # Two services naming a handler alike do not count in one entry of a shared limiter.
        async with ServiceTestHarness(
            _counting_service(shared), config=ServiceConfig(name="customers", health_port=0)
        ) as harness:
            assert (await _spend(harness))["success"] is True
        # With a namespace the service's identity on the broker is part of the key.
        recorded = _Recording()
        named = ServiceConfig(name="catalog", namespace="prod", health_port=0)
        async with ServiceTestHarness(_counting_service(recorded), config=named) as harness:
            await _spend(harness)
            await _spend(harness, "export")
        assert recorded.keys == ["prod.catalog:search:alice", "prod.catalog:export:alice"]

    asyncio.run(drive())


def _counting_before(source: Source) -> None:
    forget = run(source)["forget"]

    async def drive() -> None:
        limiter = _Recording()
        config = ServiceConfig(name="catalog", health_port=0)
        async with ServiceTestHarness(_counting_service(limiter), config=config) as harness:
            await _spent_by_alice(harness)
            await forget(limiter, "alice")
            # The caller's value alone is not the counter's key now, so the reset found nothing.
            assert (await _spend(harness))["code"] == "refused"
            assert limiter.keys[0] == "catalog:search:alice", limiter.keys

    asyncio.run(drive())


def _counting_after(source: Source) -> None:
    forget = run(source)["forget"]

    async def drive() -> None:
        limiter = _Recording()
        config = ServiceConfig(name="catalog", health_port=0)
        async with ServiceTestHarness(_counting_service(limiter), config=config) as harness:
            await _spent_by_alice(harness)
            await forget(limiter, "alice")
            assert (await _spend(harness))["success"] is True

    asyncio.run(drive())
    _counted_under_the_service_and_the_handler()


def _a_service_whose_broker_is_silent() -> tuple[CliffracerService, AsyncMock]:
    """A service whose connection reads as connected and whose drain is never answered."""
    service = CliffracerService(ServiceConfig(name="silent", health_port=0, shutdown_timeout=0.2))
    nc = AsyncMock()
    nc.is_connecting = False
    nc.is_reconnecting = False
    nc.is_closed = False
    nc.is_connected = True
    nc.is_draining = False

    async def a_drain_nothing_answers() -> None:
        await asyncio.sleep(3600)

    nc.drain = a_drain_nothing_answers
    service.container.connection.nc = nc
    return service, nc


def _stop_against_a_silent_broker(source: Source) -> tuple[Any, AsyncMock, list[str], float]:
    """The snippet's `shut_down` run on that service: what it returned, the connection, the warnings, the time."""
    shut_down = run(source)["shut_down"]
    service, nc = _a_service_whose_broker_is_silent()
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING", format="{message}")
    began = time.monotonic()
    try:
        outcome = asyncio.run(asyncio.wait_for(shut_down(service), 5))
    finally:
        logger.remove(sink)
    assert service.container.is_stopped
    return outcome, nc, lines, time.monotonic() - began


def _stop_before(source: Source) -> None:
    # The `except FlushTimeoutError` arm is dead: the stop returns, so the caller reads "clean".
    outcome, nc, _, took = _stop_against_a_silent_broker(source)
    assert outcome == "clean"
    # Upper bound. CI p99 0.203 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.2 s,
    # 804x the overshoot.
    assert took < 2.5, f"the stop took {took:.1f}s with shutdown_timeout=0.2"
    nc.close.assert_awaited_once()


def _stop_after(source: Source) -> None:
    outcome, nc, lines, took = _stop_against_a_silent_broker(source)
    assert outcome is None
    # Upper bound. CI p99 0.204 s (run 4712: eric-7, CPython 3.12.15, n=20, p99 = max); wait 0.2 s,
    # 569x the overshoot.
    assert took < 2.5, f"the stop took {took:.1f}s with shutdown_timeout=0.2"
    nc.close.assert_awaited_once()
    assert any("could not drain its NATS connection" in line for line in lines), lines
    assert any("messages still buffered may not have been sent" in line for line in lines), lines


def _a_client_whose_broker_is_reconnecting() -> tuple[ServiceClient, AsyncMock]:
    nc = AsyncMock()
    nc.is_closed = False
    nc.drain.side_effect = nats.errors.ConnectionReconnectingError()
    client = ServiceClient(service="svc", verify=False)
    client._nc = nc
    return client, nc


def _close_before(source: Source) -> None:
    # The `except ConnectionReconnectingError` arm is dead: `close()` returns, so the caller reads "closed".
    client, nc = _a_client_whose_broker_is_reconnecting()

    assert asyncio.run(run(source)["shut_down"](client)) == "closed"

    nc.close.assert_awaited_once()


def _close_after(source: Source) -> None:
    client, nc = _a_client_whose_broker_is_reconnecting()

    assert asyncio.run(run(source)["shut_down"](client)) is None

    nc.drain.assert_awaited_once()
    nc.close.assert_awaited_once()
    with pytest.raises(RpcConnectionError, match="was closed"):
        asyncio.run(client._connection())

    # Leaving `async with` while the broker redials: the block's own exception is the one raised.
    blocked, blocked_nc = _a_client_whose_broker_is_reconnecting()

    async def block() -> None:
        async with blocked:
            raise RuntimeError("the application's own failure")

    with pytest.raises(RuntimeError, match="the application's own failure") as caught:
        asyncio.run(block())
    assert caught.value.__context__ is None
    blocked_nc.close.assert_awaited_once()


def _a_service_with(connection: str) -> CliffracerService:
    """No connection at all, or one that has closed, or a live one whose requests are refused as closed."""
    service = CliffracerService(ServiceConfig(name="alone", health_port=0))
    if connection == "none":
        return service
    service.nc = AsyncMock()
    closed = nats.errors.ConnectionClosedError()
    service.nc.publish.side_effect = closed if connection == "closed" else None
    service.nc.request.side_effect = closed
    return service


def _no_connection_before(source: Source) -> None:
    # `AssertionError` is not what is raised, so the handler for it never runs.
    with pytest.raises(ServiceLifecycleError, match="not connected"):
        asyncio.run(run(source)["notify"](_a_service_with("none"), "o1"))


def _no_connection_after(source: Source) -> None:
    notify = run(source)["notify"]
    assert asyncio.run(notify(_a_service_with("none"), "o1")) == "not connected"
    assert asyncio.run(notify(_a_service_with("closed"), "o1")) == "connection lost"
    assert asyncio.run(notify(_a_service_with("request refused"), "o1")) == "connection lost"
    # The table the entry states, read from the code: no connection at all, then one that closed.
    sends = {
        "call_rpc": lambda s: s.call_rpc("other", "ping"),
        "call_async": lambda s: s.call_async("other", "ping"),
        "call_rpc_no_wait": lambda s: s.call_rpc_no_wait("other", "ping"),
        "publish_event": lambda s: s.publish_event("orders.created", order_id="o1"),
        "broadcast_message": lambda s: s.broadcast_message("orders.created", order_id="o1"),
    }
    without = dict.fromkeys(sends, RpcConnectionError) | {
        "publish_event": ServiceLifecycleError,
        "broadcast_message": ServiceLifecycleError,
    }
    for name, send in sends.items():
        for connection, expected in (("none", without[name]), ("closed", RpcConnectionError)):
            service = _a_service_with(connection)
            with pytest.raises(expected) as caught:
                asyncio.run(send(service))
            assert type(caught.value) is expected, (name, connection, caught.value)
    assert not issubclass(ServiceLifecycleError, RpcError)
    assert issubclass(RpcConnectionError, RpcError)


VALIDATION_DESCRIPTION = {
    "service": "inventory",
    "version": "1",
    "description_hash": "sha256:d",
    "methods": [{"name": "reserve", "signature_hash": "sha256:x", "params": [], "returns": None}],
}


class _Reserving(ServiceClient):
    SERVICE = "inventory"
    SIGNATURES = {"reserve": "sha256:x"}

    async def reserve(self, sku: str) -> int:
        return await self._call("reserve", {"sku": sku}, int)


def _a_client_whose_describe_fails_after_a_validation_reply(
    describe_error: Exception,
) -> ServiceClient:
    """The first describe (the lazy verify) answers; the call is then refused as invalid and the second fails."""
    describes = 0

    async def request(subject, payload, headers=None):
        nonlocal describes
        if subject.endswith("describe"):
            describes += 1
            if describes > 1:
                raise describe_error
            return SimpleNamespace(data=json.dumps(VALIDATION_DESCRIPTION).encode(), headers=None)
        return SimpleNamespace(data=json.dumps(VALIDATION_REPLY).encode(), headers=None)

    client = _Reserving()
    client._nc = AsyncMock()
    client._request = request  # type: ignore[method-assign]
    return client


DESCRIBE_FAILURES = [
    RpcTimeoutError("inventory.describe did not answer"),
    RpcConnectionError("the connection was lost"),
]


def _reverify_before(source: Source) -> None:
    # The handler for the timeout and the connection error never runs: the validation error is raised.
    reserve = run(source)["reserve"]
    for failure in DESCRIBE_FAILURES:
        client = _a_client_whose_describe_fails_after_a_validation_reply(failure)
        with pytest.raises(RpcValidationError) as caught:
            asyncio.run(reserve(client, "a1"))
        assert caught.value.details == DETAILS


def _reverify_after(source: Source) -> None:
    reserve = run(source)["reserve"]
    for failure in DESCRIBE_FAILURES:
        client = _a_client_whose_describe_fails_after_a_validation_reply(failure)
        assert asyncio.run(reserve(client, "a1")) == DETAILS


WAREHOUSE_MODULE = """
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc


class Line(BaseModel):
    sku: str
    qty: int = 1


class Warehouse(CliffracerService):
    @rpc
    async def create(self, line: Line = Line(sku="a1")) -> int:
        return 1
"""


@contextmanager
def _a_checkout_with_a_warehouse() -> Iterator[Path]:
    """A directory holding the importable package `myapp`, as the cwd and on `sys.path`."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "myapp").mkdir()
        (root / "myapp" / "__init__.py").write_text("")
        (root / "myapp" / "warehouse.py").write_text(WAREHOUSE_MODULE)
        cwd = os.getcwd()
        os.chdir(root)
        sys.path.insert(0, str(root))
        try:
            yield root
        finally:
            os.chdir(cwd)
            sys.path.remove(str(root))
            for name in [m for m in sys.modules if m == "myapp" or m.startswith("myapp.")]:
                del sys.modules[name]


def _without_rebuildable(live: Description) -> Description:
    """The description of a service that does not write `rebuildable`, hashes included."""
    methods = []
    for method in live.methods:
        params = [dataclasses.replace(p, rebuildable=None) for p in method.params]
        methods.append(
            dataclasses.replace(
                method, params=params, signature_hash=_signature_hash(params, method.returns)
            )
        )
    return dataclasses.replace(live, methods=methods)


def _a_generated_client(source: str) -> type[ServiceClient]:
    namespace: dict[str, Any] = {"__name__": "warehouse_client"}
    exec(compile(source, "<warehouse_client.py>", "exec"), namespace)
    return namespace["WarehouseClient"]


def _verifies_against(client_type: type[ServiceClient], live: Description) -> None:
    async def verify() -> None:
        client = client_type()
        client._nc = AsyncMock()

        async def request(subject, payload, headers=None):
            return SimpleNamespace(data=json.dumps(live.to_dict()).encode(), headers=None)

        client._request = request  # type: ignore[method-assign]
        await client.verify()

    asyncio.run(verify())


def _regenerate_before(source: Source) -> None:
    refused(AssertionError, "the description holds only the default's dump")(source)


def _regenerate_after(source: Source) -> None:
    with _a_checkout_with_a_warehouse() as root:
        run(source)
        written = (root / "warehouse_client.py").read_text()
        live = describe(importlib.import_module("myapp.warehouse").Warehouse)
        assert "Line.model_validate(" in written
        regenerated = _a_generated_client(written)
        assert regenerated.SIGNATURES == {m.name: m.signature_hash for m in live.methods}
        _verifies_against(regenerated, live)
        # A client generated from a service that does not carry the key is out of date against one that does.
        stale = _a_generated_client(emit(_without_rebuildable(live)))
        assert stale.SIGNATURES != regenerated.SIGNATURES
        with pytest.raises(ClientOutOfDateError, match="regenerate it"):
            _verifies_against(stale, live)


def _no_form_carries_after(source: Source) -> None:
    """The snippet's stub sends a form the receiver reads back as the value passed."""
    namespace = run(source)
    model, value = namespace["Chained"], namespace["value"]
    sent = namespace["ShopClient"](verify=False)._encode(value, model)
    assert model.model_validate(sent) == value, sent


@contextmanager
def _hashing_a_return_as_a_request_is() -> Iterator[None]:
    """`type_ref` as it hashed a return model: by the schema of what a caller may send."""
    original = typed_rpc.type_ref

    def validation_mode(tp: Any, *, mode: Any = "validation") -> dict[str, Any]:
        return original(tp, mode="validation")

    with patch.object(typed_rpc, "type_ref", validation_mode):
        yield


def _contract_hash_before(source: Source) -> None:
    refused(AssertionError, "a reply is hashed as a request")(source)


def _contract_hash_after(source: Source) -> None:
    namespace = run(source)
    shipments = namespace["Shipments"]

    def registration(revision: str) -> ServiceTemplate:
        return ServiceTemplate(
            name="shipments",
            revision=revision,
            service_class=shipments,
            settings_model=namespace["Settings"],
            factory=namespace["make_shipments"],
        )

    with _hashing_a_return_as_a_request_is():
        held = describe(shipments).method("receipt")
        assert held is not None
        catalog = TemplateCatalog()
        catalog.register(registration("warehouse-a"))
    current = describe(shipments).method("receipt")
    assert current is not None and current.signature_hash != held.signature_hash
    with pytest.raises(ActivationConflict, match="already registered"):
        catalog.register(registration("warehouse-a"))
    # The snippet's own template is accepted beside the one that holds the old contract.
    catalog.register(namespace["template"].definition)
    assert (
        catalog.resolve("shipments", "warehouse-b").contract.identity
        != catalog.resolve("shipments", "warehouse-a").contract.identity
    )


def _run_keeping(source: Source) -> tuple[dict[str, Any], BaseException | None]:
    """Run the snippet and hand back what it defined before it stopped, with what stopped it."""
    namespace: dict[str, Any] = {"__name__": "upgrading_snippet"}
    try:
        exec(compile(source, "<docs/upgrading.md>", "exec"), namespace)
    except Exception as error:
        return namespace, error
    return namespace, None


NATS_URL = "nats://orders:s3cret@broker:4222"


def _what_nats_py_sends(url: str) -> dict[str, Any]:
    """The credentials nats-py puts in its CONNECT for a server that asks for them, given the URL it dials."""
    from nats.aio.client import Client

    client = Client()
    client._setup_server_pool(str(url))  # type: ignore[attr-defined]
    client._current_server = client._server_pool[0]  # type: ignore[attr-defined]
    client._server_info = {}  # type: ignore[attr-defined]
    client._auth_configured = True  # type: ignore[attr-defined]
    client.options.update(  # type: ignore[attr-defined]
        verbose=False, pedantic=False, token=None, user=None, password=None, name=None, no_echo=None
    )
    sent = json.loads(client._connect_command().decode().split(" ", 1)[1])  # type: ignore[attr-defined]
    return {key: sent[key] for key in ("user", "pass", "auth_token") if key in sent}


def _nats_url_before(source: Source) -> None:
    namespace, error = _run_keeping(source)
    assert isinstance(error, AssertionError) and "dials the same URL" in str(error), repr(error)
    assert _what_nats_py_sends(namespace["config"].nats_url) == {"user": "orders", "pass": "s3cret"}
    # What the reloaded config hands nats-py: the mask as the token, which an authenticating broker refuses.
    assert _what_nats_py_sends(namespace["reloaded"].nats_url) == {"auth_token": "***"}


def _nats_url_after(source: Source) -> None:
    namespace = run(source)
    reloaded = namespace["reloaded"]
    assert _what_nats_py_sends(reloaded.nats_url) == {"user": "orders", "pass": "s3cret"}
    assert "s3cret" not in repr(reloaded) and "s3cret" not in reloaded.model_dump_json()
    assert isinstance(reloaded.nats_url, str) and type(reloaded.nats_url) is not str
    assert repr(reloaded.nats_url) == "'nats://***@broker:4222'"
    assert str(reloaded.nats_url) == NATS_URL and f"{reloaded.nats_url}" == NATS_URL
    # The JSON alone is what the entry says it is not enough: reloaded without the URL it dials the mask.
    from_json = ServiceConfig.model_validate_json(namespace["saved"])
    assert _what_nats_py_sends(from_json.nats_url) == {"auth_token": "***"}


def _a_service_config_in_a_run_over_the_sample_service(text: str) -> list[str]:
    """The warnings `cliffracer run` logs for the `--config` file `text`, with AlphaService as the run."""
    path = Path(tempfile.mkdtemp()) / "deploy.yaml"
    path.write_text(text)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="WARNING")
    try:
        build_orchestrator(
            ["tests.unit.cli_fixtures.sample_services:AlphaService"],
            nats_url=None,
            log_level=None,
            config_path=str(path),
        )
    finally:
        logger.remove(sink)
    return lines


def _run_config_after(source: Source) -> None:
    assert run(source)["settings"] == {"nats_user": "svc", "nats_password": "s3cret"}
    # An unused services section is reported, naming the section and the services the command runs.
    (line,) = _a_service_config_in_a_run_over_the_sample_service(
        "services:\n  alpha_servce:\n    version: '2.0.0'\n"
    )
    assert "alpha_servce" in line and "alpha_service" in line
    assert not _a_service_config_in_a_run_over_the_sample_service(
        "services:\n  alpha_service:\n    version: '2.0.0'\n"
    )


def _dlq_template_after(source: Source) -> None:
    namespace = run(source)
    build = namespace["build"]
    assert namespace["config"] is None
    assert build("dlq.{service}") is not None
    assert build("dlq.{service[0].x}") is None


def _cyanide_after(source: Source) -> None:
    orders = run(source)["Orders"]
    assert orders.cyanide.active_mode == "slow"
    with pytest.raises(ValidationError, match="add up to 1.6"):
        CyanideConfig(slow_weight=0.8, drop_reply_weight=0.8)
    with pytest.raises(ValidationError, match="slow_weight"):
        CyanideConfig(slow_weight=1.5)
    with patch.dict(os.environ, {"CLIFFRACER_CYANIDE_MODE": "slwo"}):
        with pytest.raises(ValidationError, match="unknown cyanide mode 'slwo'"):
            CyanideExtension()
    extension = CyanideExtension()
    with pytest.raises(ValueError, match="unknown cyanide mode 'slwo'"):
        extension.set_mode("slwo")
    with pytest.raises(ValueError, match="unknown cyanide mode 'slwo'"):
        extension.configure_handler("handler", "slwo")


def _a_refused_stream() -> StreamSpec:
    """A declaration that skipped the check made when it is built, as `model_construct` does."""
    return StreamSpec.model_construct(
        name="ORDERS.V1",
        subjects=["orders.v1.*"],
        storage="file",
        retention="limits",
        max_age_seconds=None,
        duplicate_window_seconds=120.0,
    )


def _stream_declaration_after(source: Source) -> None:
    orders = run(source)["orders"]
    assert orders.subjects == ["orders.*"]
    with pytest.raises(ValidationError, match="ORDERS.V1"):
        StreamSpec(name="ORDERS.V1", subjects=["orders.v1.*"])
    with pytest.raises(ValidationError, match="overlap"):
        ServiceConfig(
            name="orders",
            jetstream_streams=[{"name": "ORDERS", "subjects": ["orders.*", "orders.created"]}],
        )
    js = MagicMock()
    with pytest.raises(StreamDeclarationError, match="ORDERS.V1") as caught:
        asyncio.run(ensure_streams(js, [orders, _a_refused_stream()]))
    assert "None of the 2 declared streams was created" in str(caught.value)
    assert js.mock_calls == [], "no stream is created and the broker is not read"


def _auth_algorithm_after(source: Source) -> None:
    config = run(source)["config"]
    assert config.algorithm == "HS512"
    service = _auth_service(config)
    token = service.authenticate("alice", "a-long-enough-password")
    assert token is not None
    assert jwt.get_unverified_header(token)["alg"] == "HS512"
    assert service.validate_token(token) is not None
    for refused_algorithm in ("none", "RS256", "hs256"):
        with pytest.raises(ValidationError, match="algorithm must be one of HS256, HS384, HS512"):
            config.algorithm = refused_algorithm


class _CronBucket:
    """The one call a timer makes of a bucket when it starts: its status."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl

    async def status(self) -> Any:
        return SimpleNamespace(ttl=self.ttl)


class _CronKv:
    """The KV extension a distributed timer opens its bucket through.

    `cron_locks` is a bucket another job opened with a five minute TTL; any other bucket is created,
    as the real extension creates one, with the TTL the timer asks for.
    """

    name = "kv"

    def __init__(self) -> None:
        self.buckets = {"cron_locks": _CronBucket(300.0)}

    async def get_bucket(self, name: str, *, default_config: Any = None) -> _CronBucket:
        if name not in self.buckets:
            self.buckets[name] = _CronBucket(default_config.ttl)
        return self.buckets[name]


def _cron_service(namespace: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(name="reports", namespace=namespace), instance_id="replica-a"
    )


def _the_cron_timer(source: Source) -> tuple[DistributedCronTimer, _CronKv]:
    """The timer `@cron` made for `Reports.nightly` in the snippet, with the bucket stand-in behind it."""
    cron_timer = run(source)["Reports"].nightly._cliffracer_timers[0]
    assert isinstance(cron_timer, DistributedCronTimer)
    kv = _CronKv()
    cron_timer._explicit_kv = kv
    return cron_timer, kv


async def _started_then_stopped(cron_timer: DistributedCronTimer) -> bool:
    """Whether `start()` ran the timer, with the timer stopped again."""
    try:
        await cron_timer.start(_cron_service())
        return cron_timer.is_running
    finally:
        await cron_timer.stop()


def _cron_lease_before(source: Source) -> None:
    cron_timer, _ = _the_cron_timer(source)
    with pytest.raises(
        ConfigurationError, match=r"(?s)nightly.*lease_ttl=3600s.*'cron_locks'.*300s"
    ):
        asyncio.run(_started_then_stopped(cron_timer))


def _cron_lease_after(source: Source) -> None:
    cron_timer, kv = _the_cron_timer(source)
    assert asyncio.run(_started_then_stopped(cron_timer)), "the job starts on a bucket of its own"
    assert kv.buckets["reports_locks"].ttl == 3600.0
    assert kv.buckets["cron_locks"].ttl == 300.0, "the shared bucket is left as it was"

    # A job with `no_overlap=False` writes no lease, so a short bucket is not a reason to refuse it.
    unchecked = DistributedCronTimer(
        "0 9 * * *", distributed=True, lease_ttl=3600, no_overlap=False, kv_extension=_CronKv()
    )
    unchecked.method_name = "nightly"
    assert asyncio.run(_started_then_stopped(unchecked))


def _cron_options_after(source: Source) -> None:
    cron_timer = run(source)["Reports"].nightly._cliffracer_timers[0]
    assert isinstance(cron_timer, DistributedCronTimer) and cron_timer.lease_ttl == 600

    refused_options: list[tuple[str, dict[str, Any]]] = [
        ("lease_ttl", {"lease_ttl": value}) for value in (0, -5, math.nan, math.inf, True, "300")
    ]
    refused_options += [("no_overlap", {"no_overlap": "yes"})]
    refused_options += [("bucket", {"bucket": name}) for name in ("", "a.b", "has space")]
    for option, bad in refused_options:
        with pytest.raises(ConfigurationError, match=option):
            DistributedCronTimer("0 9 * * *", distributed=True, **bad)


class _LockBucket:
    """The calls a distributed firing makes of its bucket, with `create` the compare-and-set on absence."""

    def __init__(self) -> None:
        self.entries: dict[str, SimpleNamespace] = {}
        self._revision = 0

    def _write(self, key: str, value: bytes) -> int:
        self._revision += 1
        self.entries[key] = SimpleNamespace(value=value, revision=self._revision)
        return self._revision

    async def create(self, key: str, value: bytes) -> int:
        if key in self.entries:
            raise nats.js.errors.KeyWrongLastSequenceError()
        return self._write(key, value)

    async def get(self, key: str) -> SimpleNamespace:
        if key not in self.entries:
            raise nats.js.errors.KeyNotFoundError()
        return self.entries[key]

    async def put(self, key: str, value: bytes) -> int:
        return self._write(key, value)

    async def update(self, key: str, value: bytes, last: int | None = None) -> int:
        return self._write(key, value)

    async def delete(self, key: str, last: int | None = None) -> None:
        self.entries.pop(key, None)


_FIRING = datetime(2026, 9, 10, 9, 0, 0, tzinfo=UTC)
_EPOCH = int(_FIRING.timestamp())


def _a_replica(bucket: _LockBucket, namespace: str | None, job: Callable[[], Any]):
    """A distributed timer for `Billing.settle`, in `namespace`, that runs `job` when it wins a firing."""
    cron_timer = DistributedCronTimer("0 9 * * *", kv_extension=SimpleNamespace())
    cron_timer.method_name = "settle"
    cron_timer.service_instance = SimpleNamespace(
        instance_id="replica-a", config=SimpleNamespace(name="billing", namespace=namespace)
    )

    async def raw_bucket() -> _LockBucket:
        return bucket

    cron_timer._get_raw_bucket = raw_bucket  # type: ignore[method-assign]
    cron_timer._execute_method = job  # type: ignore[method-assign]
    return cron_timer


def _the_lease_after_clearing_it_by(source: Source) -> bool:
    """Whether the snippet's `clear_stuck_lease` removed the lease a running `app1` job holds."""
    clear = run(source)["clear_stuck_lease"]
    bucket = _LockBucket()
    seen: dict[str, bool] = {}

    async def job() -> None:
        assert "cron.app1.billing.settle.active" in bucket.entries, sorted(bucket.entries)
        await clear(bucket)
        seen["still_held"] = "cron.app1.billing.settle.active" in bucket.entries

    asyncio.run(_a_replica(bucket, "app1", job)._execute_distributed(_FIRING))
    return seen["still_held"]


def _cron_keys_before(source: Source) -> None:
    assert _the_lease_after_clearing_it_by(source), (
        "the key without the namespace clears nothing the job holds"
    )


def _cron_keys_after(source: Source) -> None:
    assert not _the_lease_after_clearing_it_by(source), "the key with the namespace clears it"

    ran: list[str] = []

    async def job() -> None:
        ran.append("ran")

    # A replica on the old version recorded the firing under the key it had.
    for namespace, expected in (("app1", 1), (None, 0)):
        ran.clear()
        bucket = _LockBucket()
        asyncio.run(bucket.create(f"cron.billing.settle.{_EPOCH}", b"{}"))
        asyncio.run(_a_replica(bucket, namespace, job)._execute_distributed(_FIRING))
        assert len(ran) == expected, (namespace, sorted(bucket.entries))


def _a_dead_letter_origin(msg: MockMessage) -> dict[str, Any]:
    fields, _ = DeadLetterPublisher(ServiceConfig(name="svc", health_port=0), lambda: None).origin(
        msg
    )
    return fields


def _mock_metadata_before(source: Source) -> None:
    namespace = run(source)
    # A message given no metadata raises, where the snippet's test read it as `None`.
    with pytest.raises(NotJSMessageError):
        namespace["has_metadata"](MockMessage("orders.created"))
    # A sequence given as a number is not the pair a dead letter reads its stream sequence from.
    assert "stream_sequence" not in _a_dead_letter_origin(namespace["a_delivery"]())


def _mock_metadata_after(source: Source) -> None:
    namespace = run(source)
    assert namespace["has_metadata"](MockMessage("orders.created")) is False
    delivery = namespace["a_delivery"]()
    assert namespace["has_metadata"](delivery) is True
    assert isinstance(delivery.metadata.sequence, Msg.Metadata.SequencePair)
    assert isinstance(MockJetStreamMetadata().sequence, Msg.Metadata.SequencePair)
    assert _a_dead_letter_origin(delivery)["stream_sequence"] == 7


ROWS: dict[str, tuple[Check, Check]] = {
    "A service's response grant outlasts its deadline reply": (
        refused(ValueError, r"response_ttl=2.0s does not cover max_rpc_processing_time=2.0s"),
        clean,
    ),
    "A config names one way to authenticate to NATS": (
        refused(ValidationError, "more than one way to authenticate to NATS"),
        _two_nats_credentials_after,
    ),
    "`ServiceConfig.nats_password` and `nats_token` are `SecretStr`": (
        refused(AttributeError, "upper"),
        _service_config_secret_after,
    ),
    "`stop()` returns when the broker does not answer the connection's drain": (
        _stop_before,
        _stop_after,
    ),
    "`close()` and leaving `async with` release a connection that is reconnecting": (
        _close_before,
        _close_after,
    ),
    "A send with no connection raises `RpcConnectionError` or `ServiceLifecycleError`": (
        _no_connection_before,
        _no_connection_after,
    ),
    "A failed re-verify does not replace a validation error": (
        _reverify_before,
        _reverify_after,
    ),
    "A generated client is regenerated for a model default": (
        _regenerate_before,
        _regenerate_after,
    ),
    "A value no form carries is refused before it is sent": (
        refused(
            RpcValidationError,
            "refused before sending: Chained would arrive with a read as b's value, b read as c's value",
        ),
        _no_form_carries_after,
    ),
    "A return model's contract hash is the hash of what it writes": (
        _contract_hash_before,
        _contract_hash_after,
    ),
    "A cross-namespace listener needs a namespace": (
        refused_at_startup("Audit", ConfigurationError, "has no namespace to span"),
        starts("Audit"),
    ),
    "A handler is not named for a method the framework calls": (
        refused_at_startup("Worker", ConfigurationError, "health_check"),
        starts("Worker"),
    ),
    "A timer takes an interval the loop can run": (
        refused(ConfigurationError, "interval must be a finite number"),
        _a_zero_drift_and_backoff_are_allowed,
    ),
    "A service configured for msgpack has the package": (_msgpack_before, _msgpack_after),
    "`start()` raises when the service was stopped while it was starting": (
        _stopped_while_starting_before,
        _stopped_while_starting_after,
    ),
    "A durable listener has a stream that carries its subject": (
        _durable_refused_at_startup,
        starts("Billing"),
    ),
    "A stream subject does not begin with a wildcard": (
        refused(ValidationError, "(?s)begins with a wildcard.*10052"),
        _stream_subjects,
    ),
    "A `ServiceConfig` does not print or dump the password in its `nats_url`": (
        _nats_url_before,
        _nats_url_after,
    ),
    "`cliffracer run --config` refuses a key it would drop": (
        refused(ConfigError, "unknown top-level key 'my_service'"),
        _run_config_after,
    ),
    "A bad `dlq_subject` template is a `ValidationError`": (
        refused(ValidationError, "dlq_subject cannot be rendered"),
        _dlq_template_after,
    ),
    "`CyanideConfig` refuses a setting it cannot carry out": (
        refused(ValidationError, "unknown cyanide mode 'slwo'"),
        _cyanide_after,
    ),
    "A stream declaration is refused when it is built": (
        refused(ValidationError, "(?s)stream 'ORDERS' cannot be declared.*overlap"),
        _stream_declaration_after,
    ),
    "`AuthConfig.algorithm` is one the service can sign with": (
        refused(ValidationError, "algorithm must be one of HS256, HS384, HS512"),
        _auth_algorithm_after,
    ),
    "`call_rpc` raises the typed errors the standalone client raises": (
        _call_rpc_before,
        _call_rpc_after,
    ),
    "`call_rpc` raises `RpcNoRespondersError` when nothing is subscribed": (
        _no_responders_before,
        _no_responders_after,
    ),
    "A send raises an `RpcError` for what the connection raises": (
        _send_error_before,
        _send_error_after,
    ),
    "`cliffracer.core.typed_rpc.python_type` is gone": (
        refused(ImportError, "python_type"),
        _python_type_after,
    ),
    "`AuthConfig.secret_key` is a `SecretStr`": (
        refused(AttributeError, "encode"),
        _secret_after,
    ),
    "A refresh stops 30 days after the login": (_refresh_before, _refresh_after),
    "A token longer-lived than this service's own is refused": (
        _longer_token_before,
        _longer_token_after,
    ),
    "`BucketConfig` and `ObjectStoreConfig` check their options when built": (
        refused(BucketConfigError, "sessions.*history"),
        _bucket_after,
    ),
    "A KV declaration that cannot work is refused by name": (
        _kv_declared_twice_before,
        _kv_declared_once_after,
    ),
    "A KV write stores JSON or raises": (_kv_put_before, _kv_put_after),
    "`delete()` and `purge()` return `None`": (_kv_delete_before, _kv_delete_after),
    "A bucket is declared on the extension, not on a config subclass": (
        _kv_buckets_before,
        _kv_buckets_after,
    ),
    "A KV write refuses a model its class does not read back as itself": (
        _kv_model_before,
        _kv_model_after,
    ),
    "A circuit breaker config is checked when it is built": (
        refused(TypeError, "monitored_exceptions must be a tuple or list"),
        _breaker_after,
    ),
    "The rate limiter's bucket carries the subject prefix": (
        _rate_limiter_before,
        _rate_limiter_after,
    ),
    "A latency percentile is a nearest-rank sample": (
        refused(AssertionError, "one slow request"),
        _percentile_after,
    ),
    "`active_connections` goes up and down": (
        refused(ValueError, "active_connections is a level"),
        clean,
    ),
    "`BatchProcessor.add_item` says what each caller receives": (_batch_before, _batch),
    "Readiness asks the broker, not only nats-py's flag": (
        refused(AssertionError, "reads the flag, not the broker"),
        clean,
    ),
    "A request without its rate-limit key is refused, not answered `internal`": (
        _missing_key_before,
        _missing_key_after,
    ),
    "A declared extension runs before the payload is validated": (
        _validation_order_before,
        _validation_order_after,
    ),
    "A reply carries no request headers": (_headers_before, _headers_after),
    "A handler parameter takes no alias": (_alias_before, _alias_after),
    "A rate limit that cannot work is refused where it is declared": (
        _a_limit_refused_where_declared_before,
        _a_limit_refused_where_declared_after,
    ),
    "A rate limit counts per service and per handler": (_counting_before, _counting_after),
    "An explicit `idempotency_key` carries no ordinal inside `@idempotent`": (
        refused(AssertionError, "an explicit key is used as given"),
        clean,
    ),
    "A distributed cron job's lease fits its bucket's TTL": (
        _cron_lease_before,
        _cron_lease_after,
    ),
    "A distributed cron job's options are checked where it is declared": (
        refused(ConfigurationError, "lease_ttl must be a finite number of seconds above zero"),
        _cron_options_after,
    ),
    "A distributed cron job's lock keys carry the service's namespace": (
        _cron_keys_before,
        _cron_keys_after,
    ),
    "`MockMessage.metadata` raises without metadata, and the mock sequence is a `SequencePair`": (
        _mock_metadata_before,
        _mock_metadata_after,
    ),
    "pydantic 2.11 or later is required": (
        refused(AssertionError, "does not satisfy"),
        clean,
    ),
    "The `cliffracer-faststream`, `cliffracer-backdoor` and `cliffracer-http` distributions are gone": (
        refused(ImportError, "cliffracer_http"),
        clean,
    ),
    "An inbound span is named for the handler, an event is a consumer span, a timer is internal, and `describe` has none": (
        lambda source: _run_over_spans(source, refused_message="named for the handler now"),
        lambda source: _run_over_spans(source),
    ),
}


# ---- the tests --------------------------------------------------------------------------------


def test_the_page_is_read_and_every_entry_has_a_row():
    found = sections()

    assert len(found) >= 15, (
        f"read {len(found)} entries from {UPGRADING}; the parser is reading nothing"
    )
    assert set(found) == set(ROWS), (
        f"on the page and not in the table: {sorted(set(found) - set(ROWS))}; "
        f"in the table and not on the page: {sorted(set(ROWS) - set(found))}"
    )


@pytest.mark.parametrize("title", ROWS)
def test_the_before_fails_in_the_way_its_entry_says(title: str):
    before, _ = ROWS[title]

    before(sections()[title]["before"])


@pytest.mark.parametrize("title", ROWS)
def test_the_after_runs_and_does_what_its_entry_says(title: str):
    _, after = ROWS[title]

    after(sections()[title]["after"])


@pytest.mark.parametrize("title", ROWS)
def test_CONTROL_a_row_rejects_the_other_snippet(title: str):
    """Each check is read against the wrong snippet: the "before" check must refuse an "after", and
    the "after" check a "before". A row that accepted either would pass whatever the page said."""
    before, after = ROWS[title]
    snippets = sections()[title]

    with pytest.raises(BaseException) as accepted:
        before(snippets["after"])
    assert not isinstance(accepted.value, KeyboardInterrupt)
    with pytest.raises(BaseException) as accepted:
        after(snippets["before"])
    assert not isinstance(accepted.value, KeyboardInterrupt)


def test_CONTROL_the_parser_refuses_an_entry_missing_its_after():
    page = "### An entry\n\n```py\nx = 1\n```\n"

    with pytest.raises(AssertionError, match="needs one each"):
        sections(page)
