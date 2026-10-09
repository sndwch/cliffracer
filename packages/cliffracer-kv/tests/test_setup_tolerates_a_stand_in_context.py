"""The KV extension sets up against a context that is not a real one.

Not every caller hands this extension an `ExtensionSetupContext`. The benchmark
harness builds `types.SimpleNamespace(nc=nc, js=js)` and passes that, so any
attribute the extension reads off the context unconditionally is an
`AttributeError` in a tier that only CI runs.
"""

import types
from typing import Any, cast

import pytest
from cliffracer_kv import KvExtension

pytestmark = pytest.mark.unit


def _benchmark_shaped_context() -> Any:
    """Exactly what tests/benchmark/benchmarks.py hands the extension."""
    return cast(Any, types.SimpleNamespace(nc=object(), js=object()))


async def test_setup_survives_a_context_with_no_service_config():
    """The shape the benchmark passes: a namespace carrying only nc and js."""
    ext = KvExtension()

    await ext.setup(_benchmark_shaped_context())

    assert ext._subject_prefix is None


async def test_setup_reads_the_prefix_when_a_config_is_there():
    """And a real context still supplies it, so the tolerance costs nothing."""
    ext = KvExtension()
    config = types.SimpleNamespace(subject_prefix="w7")
    ctx = cast(Any, types.SimpleNamespace(nc=object(), js=object(), service_config=config))

    await ext.setup(ctx)

    assert ext._subject_prefix == "w7"


def test_CONTROL_the_stand_in_really_lacks_the_attribute():
    """Or the test above passes by describing a context that has one."""
    assert not hasattr(_benchmark_shaped_context(), "service_config")


async def test_a_service_config_attribute_named_kv_buckets_is_not_a_declaration():
    """`ServiceConfig` has no such field, so a bucket list on it was never a documented way to
    declare buckets; the extension reads its constructor's declarations and nothing else."""
    ext = KvExtension()
    config = types.SimpleNamespace(subject_prefix=None, kv_buckets=["from_config"])
    ctx = cast(Any, types.SimpleNamespace(nc=object(), js=object(), service_config=config))

    await ext.setup(ctx)

    assert ext._bucket_configs == {}
