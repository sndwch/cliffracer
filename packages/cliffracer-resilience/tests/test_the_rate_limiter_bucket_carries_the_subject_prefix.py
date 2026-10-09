"""The rate limiter's bucket is named the way every other bucket on the broker is.

`KvExtension` puts the service's subject prefix in front of a bucket's name (`px_mine`), which is
what keeps two environments, or the isolated prefix a test session takes, off each other's
buckets. `KvRateLimiter` opened `rate_limits` with no prefix, so its counters were shared across
every prefix and namespace on a broker: a limit consumed in one environment was consumed in all of
them. The extension that opens the default limiter now hands it the service's prefix.

A limiter that a caller opens itself (a `kv=` it was given, or an `init_kv(js=...)` call with no
service involved) keeps the name its owner chose.
"""

from __future__ import annotations

from types import SimpleNamespace

import nats.js.errors
import pytest
from cliffracer_resilience import ResilienceExtension
from cliffracer_resilience.rate_limiter import KvRateLimiter

pytestmark = pytest.mark.unit


class _Js:
    """Records the bucket names it is asked to open or create; every bucket is missing at first."""

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.created: list[str] = []

    async def key_value(self, bucket):
        self.opened.append(bucket)
        raise nats.js.errors.BucketNotFoundError

    async def create_key_value(self, **params):
        self.created.append(params["bucket"])
        return object()


def _context(prefix, js):
    return SimpleNamespace(
        service=SimpleNamespace(js=js), service_config=SimpleNamespace(subject_prefix=prefix)
    )


@pytest.mark.parametrize(
    ("prefix", "bucket", "expected"),
    [
        ("px", "rate_limits", "px_rate_limits"),
        ("ci42", "rate_limits", "ci42_rate_limits"),
        ("px", "my_limits", "px_my_limits"),
        (None, "rate_limits", "rate_limits"),
        ("", "rate_limits", "rate_limits"),
    ],
    ids=["default", "another-prefix", "named-bucket", "no-prefix", "empty-prefix"],
)
async def test_the_extension_opens_the_limiters_bucket_under_the_service_prefix(
    prefix, bucket, expected
):
    js = _Js()
    extension = ResilienceExtension(limiter=KvRateLimiter(bucket_name=bucket))
    bound = extension.bind(service=None, name="resilience")

    await bound.setup(_context(prefix, js))

    assert js.opened == [expected]
    assert js.created == [expected]


async def test_two_services_with_different_prefixes_use_different_buckets():
    one, two = _Js(), _Js()
    for js, prefix in ((one, "dev"), (two, "prod")):
        bound = ResilienceExtension(limiter=KvRateLimiter()).bind(service=None, name="resilience")
        await bound.setup(_context(prefix, js))

    assert one.created == ["dev_rate_limits"] and two.created == ["prod_rate_limits"]


async def test_CONTROL_a_limiter_the_caller_opens_itself_keeps_the_name_it_was_given():
    js = _Js()
    limiter = KvRateLimiter(js=js, bucket_name="rate_limits")

    await limiter.init_kv()

    assert js.opened == ["rate_limits"] and js.created == ["rate_limits"]


async def test_CONTROL_a_limiter_given_a_bucket_directly_opens_nothing():
    js = _Js()
    limiter = KvRateLimiter(kv=object())
    extension = ResilienceExtension(limiter=limiter)
    bound = extension.bind(service=None, name="resilience")

    await bound.setup(_context("px", js))

    assert js.opened == [] and js.created == []


async def test_the_declared_bucket_name_is_still_the_name_the_caller_gave():
    limiter = KvRateLimiter(bucket_name="my_limits")
    extension = ResilienceExtension(limiter=limiter)
    bound = extension.bind(service=None, name="resilience")
    await bound.setup(_context("px", _Js()))

    assert bound.limiter.bucket_name == "my_limits"
    assert bound.limiter.bucket_wire_name == "px_my_limits"


async def test_the_bucket_another_replica_created_first_is_opened_under_the_same_prefixed_name():
    """Two replicas race to create it: the one that loses opens what the winner made."""

    class RacingJs:
        def __init__(self) -> None:
            self.opened: list[str] = []
            self.created: list[str] = []

        async def key_value(self, bucket):
            self.opened.append(bucket)
            if len(self.opened) == 1:
                raise nats.js.errors.BucketNotFoundError
            return object()

        async def create_key_value(self, **params):
            self.created.append(params["bucket"])
            raise nats.js.errors.KeyWrongLastSequenceError

    js = RacingJs()
    bound = ResilienceExtension(limiter=KvRateLimiter()).bind(service=None, name="resilience")

    await bound.setup(_context("px", js))

    assert js.created == ["px_rate_limits"]
    assert js.opened == ["px_rate_limits", "px_rate_limits"]
    assert bound.limiter._kv is not None


# One limiter names one bucket. A limiter given at declaration is shared, not copied, by every
# service that is built from it, so a second prefix would put one service in the other's bucket.


async def test_a_limiter_shared_by_services_with_different_prefixes_is_refused_at_setup():
    from cliffracer.core.exceptions import ConfigurationError

    shared = KvRateLimiter()
    first, second = _Js(), _Js()
    one = ResilienceExtension(limiter=shared).bind(service=None, name="resilience")
    two = ResilienceExtension(limiter=shared).bind(service=None, name="resilience")
    await one.setup(_context("dev", first))

    with pytest.raises(ConfigurationError) as caught:
        await two.setup(_context("prod", second))

    message = str(caught.value)
    assert "'dev'" in message and "'prod'" in message and "its own KvRateLimiter" in message
    assert second.opened == [] and second.created == [], "the refused service opened nothing"
    assert shared.bucket_wire_name == "dev_rate_limits", "the first service keeps its bucket"


async def test_the_same_prefix_again_is_not_a_conflict():
    shared = KvRateLimiter()
    for _ in range(3):
        bound = ResilienceExtension(limiter=shared).bind(service=None, name="resilience")
        await bound.setup(_context("px", _Js()))

    assert shared.bucket_wire_name == "px_rate_limits"


@pytest.mark.parametrize(("first", "second"), [(None, "px"), ("px", None), ("", "px")])
async def test_having_no_prefix_is_a_prefix_for_this_purpose(first, second):
    from cliffracer.core.exceptions import ConfigurationError

    shared = KvRateLimiter()
    await (
        ResilienceExtension(limiter=shared)
        .bind(service=None, name="r")
        .setup(_context(first, _Js()))
    )

    with pytest.raises(ConfigurationError):
        await (
            ResilienceExtension(limiter=shared)
            .bind(service=None, name="r")
            .setup(_context(second, _Js()))
        )


async def test_CONTROL_two_limiters_for_two_prefixes_are_fine():
    for prefix in ("dev", "prod"):
        limiter = KvRateLimiter()
        bound = ResilienceExtension(limiter=limiter).bind(service=None, name="resilience")
        await bound.setup(_context(prefix, _Js()))
        assert limiter.bucket_wire_name == f"{prefix}_rate_limits"


@pytest.mark.parametrize(("first", "second"), [(None, ""), ("", None)])
async def test_no_prefix_and_an_empty_prefix_are_the_same_prefix_and_not_a_conflict(first, second):
    """Both name the unprefixed bucket, so a limiter shared between them is not shared across any
    boundary and must not be refused."""
    shared = KvRateLimiter()
    one, two = _Js(), _Js()
    await (
        ResilienceExtension(limiter=shared).bind(service=None, name="r").setup(_context(first, one))
    )

    await (
        ResilienceExtension(limiter=shared)
        .bind(service=None, name="r")
        .setup(_context(second, two))
    )

    assert one.created == ["rate_limits"], "the first service opened the unprefixed bucket"
    assert two.created == [], "the second found the shared limiter already open on it"
    assert shared.bucket_wire_name == "rate_limits"
