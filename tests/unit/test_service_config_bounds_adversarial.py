"""Adversarial validation and boundary tests for ServiceConfig.

Tests verify strict numeric bounds, type rejection, and assignment validation
across the numeric, delay, name, namespace and `dlq_subject` fields, ensuring non-positive
concurrency limits, negative timeouts, and illegal runtime mutations raise
pydantic.ValidationError immediately and preserve instance integrity.

The free-text `nats_url` and `health_host` are among them too: a value nats-py cannot connect to
or asyncio cannot bind is refused at construction, with the reasons pinned in
`test_a_nats_url_and_a_health_host_that_cannot_work_are_refused.py`.
"""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from cliffracer.core.container import Container
from cliffracer.core.service_config import ServiceConfig

pytestmark = pytest.mark.unit

CONCURRENCY_FIELDS = [
    "max_rpc_concurrency",
    "max_event_concurrency",
    "max_async_rpc_concurrency",
]

TIMEOUT_GT_ZERO_FIELDS = [
    "connect_timeout",
    "shutdown_timeout",
    "request_timeout",
    "jetstream_ack_wait",
    "jetstream_pull_timeout",
]

NON_NEGATIVE_DELAY_FIELDS = [
    "jetstream_nak_backoff",
    "jetstream_max_backoff",
    "restart_delay",
]


@pytest.mark.parametrize("field", CONCURRENCY_FIELDS)
@pytest.mark.parametrize(
    "invalid_val",
    [
        0,
        -1,
        -999,
        0.0,
        -1.0,
        1.5,
        float("inf"),
        float("-inf"),
        float("nan"),
        "0",
        "-1",
        "concurrency",
        "",
        False,
        [],
        {},
    ],
)
def test_concurrency_bounds_reject_invalid_instantiation(field: str, invalid_val: Any) -> None:
    """Instantiation with non-positive, float, string, or illegal types raises ValidationError."""
    with pytest.raises(ValidationError):
        ServiceConfig(**{"name": "adversarial_test", field: invalid_val})


@pytest.mark.parametrize("field", CONCURRENCY_FIELDS)
@pytest.mark.parametrize("valid_val", [1, 2, 50, 1000, None])
def test_concurrency_bounds_accept_valid_instantiation(field: str, valid_val: int | None) -> None:
    """Instantiation with positive integers or None succeeds."""
    cfg = ServiceConfig(**{"name": "adversarial_test", field: valid_val})  # type: ignore[arg-type]
    assert getattr(cfg, field) == valid_val


@pytest.mark.parametrize("field", TIMEOUT_GT_ZERO_FIELDS)
@pytest.mark.parametrize(
    "invalid_val",
    [
        0,
        0.0,
        -0.0001,
        -1.0,
        -100.0,
        float("-inf"),
        float("nan"),
        "0",
        "-1.0",
        "slow",
        "",
        False,
        [],
        {},
    ],
)
def test_timeouts_reject_zero_and_negative_instantiation(field: str, invalid_val: Any) -> None:
    """Instantiation with zero or negative timeout values raises ValidationError."""
    with pytest.raises(ValidationError):
        ServiceConfig(**{"name": "adversarial_test", field: invalid_val})


@pytest.mark.parametrize("field", NON_NEGATIVE_DELAY_FIELDS)
@pytest.mark.parametrize(
    "invalid_val",
    [
        -0.001,
        -1.0,
        -50.0,
        float("-inf"),
        float("nan"),
        "-0.5",
        "invalid",
        [],
        {},
    ],
)
def test_delays_reject_negative_instantiation(field: str, invalid_val: Any) -> None:
    """Instantiation with negative delay values raises ValidationError."""
    with pytest.raises(ValidationError):
        ServiceConfig(**{"name": "adversarial_test", field: invalid_val})


@pytest.mark.parametrize("field", NON_NEGATIVE_DELAY_FIELDS)
def test_delays_accept_zero_and_positive_instantiation(field: str) -> None:
    """Zero delay values are accepted for non-negative delay fields."""
    cfg = ServiceConfig(**{"name": "adversarial_test", field: 0.0})  # type: ignore[arg-type]
    assert getattr(cfg, field) == 0.0


@pytest.mark.parametrize(
    ("field", "invalid_vals"),
    [
        ("max_reconnect_attempts", [-2, -10, 1.5, "infinite", []]),
        ("reconnect_time_wait", [-1, -10, 1.5, "instant", []]),
        ("jetstream_max_deliver", [0, -1, -10, 1.5, "many", False, []]),
        ("jetstream_max_ack_pending", [0, -1, -10, 1.5, "pending", False, []]),
        ("jetstream_pull_batch", [0, -1, -10, 1.5, "batch", False, []]),
        ("health_port", [-1, 65536, 100000, 80.5, "port", None, []]),
        ("serialization_format", ["xml", "protobuf", 123, None]),
        ("default_on_invalid", ["ignore", "retry", 42, None]),
        (
            "name",
            [
                "",
                "   ",
                "service name",
                "svc*wildcard",
                "svc>wildcard",
                "svc..dot",
                ".svc",
                "svc.",
                123,
                None,
            ],
        ),
        ("namespace", ["", "ns.dot", "ns*wildcard", "ns>wildcard", "ns space", 123]),
        ("nats_url", ["", "   ", "http://not-nats", "nats://", "nats://h:notaport", 123, None]),
        ("health_host", ["not a host", "bad/host", "a..b", "[::1]", 123, None]),
        # A template rather than a subject, rendered straight into the dead-letter publish: an
        # illegal result is refused here, because a publish to one raises nothing client-side and
        # the dead letters routed to it would vanish.
        (
            "dlq_subject",
            [
                "",
                "dlq with spaces.*>",
                ".dlq.{service}",
                "dlq.{service}.",
                "dlq..{service}",
                "dlq.{nope}",
                "{namespace}.dlq.{service}",  # no namespace set: renders `.dlq.valid_name`
                123,
                None,
            ],
        ),
    ],
)
def test_specialized_field_bounds_reject_invalid_instantiation(
    field: str, invalid_vals: list[Any]
) -> None:
    """Specialized fields reject out-of-range, non-conforming, or mistyped arguments at initialization."""
    for val in invalid_vals:
        with pytest.raises(ValidationError):
            ServiceConfig(**{"name": "valid_name", field: val})


def test_extra_fields_forbidden_at_instantiation() -> None:
    """Passing undeclared keyword arguments raises ValidationError due to extra='forbid'."""
    with pytest.raises(ValidationError):
        ServiceConfig(name="valid_name", **{"unregistered_field": 123})  # type: ignore[arg-type]


def test_extra_fields_forbidden_at_assignment() -> None:
    """Assigning undeclared attributes raises ValidationError post-instantiation."""
    cfg = ServiceConfig(name="valid_name")
    with pytest.raises(ValidationError):
        cfg.unknown_runtime_attribute = "bad"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("field", "invalid_val", "original_val"),
    [
        ("max_rpc_concurrency", 0, None),
        ("max_rpc_concurrency", -5, None),
        ("max_rpc_concurrency", "abc", None),
        ("max_rpc_concurrency", 1.5, None),
        ("max_rpc_concurrency", False, None),
        ("max_event_concurrency", 0, None),
        ("max_event_concurrency", -1, None),
        ("max_async_rpc_concurrency", 0, None),
        ("max_async_rpc_concurrency", -1, None),
        ("connect_timeout", 0.0, 30.0),
        ("connect_timeout", -1.0, 30.0),
        ("connect_timeout", "slow", 30.0),
        ("shutdown_timeout", 0.0, 30.0),
        ("shutdown_timeout", -5.0, 30.0),
        ("request_timeout", 0.0, 30.0),
        ("request_timeout", -1.0, 30.0),
        ("jetstream_ack_wait", 0.0, 30.0),
        ("jetstream_ack_wait", -10.0, 30.0),
        ("jetstream_pull_timeout", 0.0, 5.0),
        ("jetstream_pull_timeout", -1.0, 5.0),
        ("jetstream_nak_backoff", -0.1, 1.0),
        ("jetstream_max_backoff", -1.0, 60.0),
        ("restart_delay", -0.5, 1.0),
        ("reconnect_time_wait", -1, 2),
        ("max_reconnect_attempts", -2, -1),
        ("health_port", -1, 8000),
        ("health_port", 65536, 8000),
        ("serialization_format", "avro", "json"),
        ("default_on_invalid", "drop_all", "deadletter"),
        ("name", "", "valid_name"),
        ("name", "name with spaces", "valid_name"),
        ("name", "name*star", "valid_name"),
        ("name", "name..doubledot", "valid_name"),
        ("namespace", "ns.dot", None),
        ("namespace", "ns*star", None),
    ],
)
def test_post_instantiation_mutations_reject_invalid_and_preserve_state(
    field: str, invalid_val: Any, original_val: Any
) -> None:
    """Mutating any field to an invalid value raises ValidationError and leaves previous value intact."""
    cfg = ServiceConfig(name="valid_name")
    assert getattr(cfg, field) == original_val

    with pytest.raises(ValidationError):
        setattr(cfg, field, invalid_val)

    assert getattr(cfg, field) == original_val


def test_post_instantiation_mutations_accept_valid_values() -> None:
    """Mutating fields to valid alternative values updates instance state."""
    cfg = ServiceConfig(name="valid_name")

    cfg.max_rpc_concurrency = 15
    assert cfg.max_rpc_concurrency == 15

    cfg.max_rpc_concurrency = None
    assert cfg.max_rpc_concurrency is None

    cfg.max_event_concurrency = 8
    assert cfg.max_event_concurrency == 8

    cfg.max_async_rpc_concurrency = 4
    assert cfg.max_async_rpc_concurrency == 4

    cfg.connect_timeout = 5.5
    assert cfg.connect_timeout == 5.5

    cfg.connect_timeout = None
    assert cfg.connect_timeout is None

    cfg.health_port = 0
    assert cfg.health_port == 0

    cfg.health_port = 65535
    assert cfg.health_port == 65535

    cfg.namespace = "tenant"
    assert cfg.namespace == "tenant"

    cfg.namespace = None
    assert cfg.namespace is None


async def _permits_that_can_be_taken(sem: asyncio.Semaphore, ceiling: int = 10) -> int:
    """How many permits `sem` hands out before it blocks, read through `acquire()` and not `_value`."""
    taken = 0
    while taken < ceiling:
        try:
            await asyncio.wait_for(sem.acquire(), timeout=0.02)
        except TimeoutError:
            break
        taken += 1
    return taken


async def test_container_derives_distinct_cached_semaphores_that_bound_executions() -> None:
    """Three semaphores from three configured limits, each created once and then handed back, and
    each blocking at its limit. A getter that built a new semaphore per call would satisfy a read
    of a fresh semaphore's counter and throttle nothing, and the counter is asyncio's private
    state besides: what is asserted here is what `acquire()` does."""
    service = MagicMock()
    service.name = "concurrency_service"

    cfg = ServiceConfig(
        name="concurrency_service",
        max_rpc_concurrency=3,
        max_event_concurrency=4,
        max_async_rpc_concurrency=2,
    )
    container = Container(service, cfg)

    sem_rpc = container._get_rpc_semaphore()
    sem_event = container._get_event_semaphore()
    sem_async = container._get_async_rpc_semaphore()

    # Created once: the same object on every later call, and a different one per kind
    assert container._get_rpc_semaphore() is sem_rpc
    assert container._get_event_semaphore() is sem_event
    assert container._get_async_rpc_semaphore() is sem_async
    assert len({id(sem_rpc), id(sem_event), id(sem_async)}) == 3

    # Each blocks at its own configured limit
    assert sem_rpc is not None and sem_event is not None and sem_async is not None
    assert await _permits_that_can_be_taken(sem_rpc) == 3
    assert await _permits_that_can_be_taken(sem_event) == 4
    assert await _permits_that_can_be_taken(sem_async) == 2
    assert sem_rpc.locked() and sem_event.locked() and sem_async.locked()


def test_container_concurrency_fallback_to_rpc_limit() -> None:
    """Container falls back to max_rpc_concurrency for async RPC semaphore if max_async_rpc_concurrency is None."""
    service = MagicMock()
    service.name = "fallback_service"

    cfg = ServiceConfig(
        name="fallback_service",
        max_rpc_concurrency=6,
        max_async_rpc_concurrency=None,
    )
    container = Container(service, cfg)

    sem_async = container._get_async_rpc_semaphore()
    assert sem_async is not None
    assert sem_async._value == 6


def test_container_concurrency_unbounded_when_none() -> None:
    """Container sets semaphores to None when concurrency limits are None."""
    service = MagicMock()
    service.name = "unbounded_service"

    cfg = ServiceConfig(
        name="unbounded_service",
        max_rpc_concurrency=None,
        max_event_concurrency=None,
        max_async_rpc_concurrency=None,
    )
    container = Container(service, cfg)

    assert container._get_rpc_semaphore() is None
    assert container._get_event_semaphore() is None
    assert container._get_async_rpc_semaphore() is None


@pytest.mark.asyncio
async def test_container_empirical_rpc_concurrency_throttling() -> None:
    """Empirical load test verifying concurrent RPC executions never exceed configured limit."""
    service = MagicMock()
    service.name = "throttled_rpc_service"

    active_count = 0
    max_observed = 0
    lock = asyncio.Lock()

    async def mock_handle_rpc(msg: Any, **_dispatch: Any) -> None:
        nonlocal active_count, max_observed
        async with lock:
            active_count += 1
            if active_count > max_observed:
                max_observed = active_count
        await asyncio.sleep(0.02)
        async with lock:
            active_count -= 1

    service._handle_rpc_request = mock_handle_rpc

    cfg = ServiceConfig(name="throttled_rpc_service", max_rpc_concurrency=2)
    container = Container(service, cfg)
    # The handler the subscribed callback spawns, so the bound read is the live one.
    container.dispatcher.rpc.handle_rpc_request = mock_handle_rpc  # type: ignore[method-assign]

    tasks = [
        asyncio.create_task(container.dispatcher.on_rpc_request(MagicMock())) for _ in range(8)
    ]
    await asyncio.gather(*tasks)
    await asyncio.gather(*container._active_tasks)

    assert max_observed == 2


@pytest.mark.asyncio
async def test_container_empirical_event_concurrency_throttling() -> None:
    """Empirical load test verifying concurrent event executions never exceed configured limit."""
    service = MagicMock()
    service.name = "throttled_event_service"

    active_count = 0
    max_observed = 0
    lock = asyncio.Lock()

    async def mock_dispatch_event(
        msg: Any, pattern: str | None = None, raise_on_error: bool = False
    ) -> None:
        nonlocal active_count, max_observed
        async with lock:
            active_count += 1
            if active_count > max_observed:
                max_observed = active_count
        await asyncio.sleep(0.02)
        async with lock:
            active_count -= 1

    service.container = None
    cfg = ServiceConfig(name="throttled_event_service", max_event_concurrency=3)
    container = Container(service, cfg)
    # The handler the subscribed callback spawns, so the bound read is the live one.
    container.dispatcher.events.handle_event = mock_dispatch_event  # type: ignore[method-assign,assignment]

    callback = container.dispatcher.make_event_callback("orders.*")

    async def invoke_callback() -> None:
        await callback(MagicMock())

    tasks = [asyncio.create_task(invoke_callback()) for _ in range(10)]
    await asyncio.gather(*tasks)
    await asyncio.gather(*container._active_tasks)

    assert max_observed == 3
