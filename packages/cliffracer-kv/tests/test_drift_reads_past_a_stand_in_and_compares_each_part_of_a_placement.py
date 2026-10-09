"""What `bucket_drift` reads as drift.

An attribute that is not shaped like the option it stands for (a republish rule whose source or
destination is not text, a placement whose cluster is not text or whose tags are not a list, a
description that is not text, an `allow_direct` that is not a bool) is a stand-in, not drift. A rule
without `headers_only` reads as not headers-only. A placement of tags alone, or of a cluster alone,
is compared.
"""

from types import SimpleNamespace

import pytest
from cliffracer_kv.config import BucketConfig
from cliffracer_kv.drift import bucket_drift
from nats.js.api import Placement, RePublish

pytestmark = pytest.mark.unit

DECLARED_REPUBLISH = BucketConfig(name="b", republish=RePublish(src=">", dest="m.>"))


@pytest.mark.parametrize(
    "rule",
    [
        SimpleNamespace(src=5, dest=6),
        SimpleNamespace(src=">", dest=6),
        SimpleNamespace(src=5, dest="m.>"),
    ],
    ids=["neither-is-text", "dest-is-not-text", "src-is-not-text"],
)
def test_a_republish_rule_not_shaped_like_one_is_not_drift(rule):
    assert bucket_drift(DECLARED_REPUBLISH, SimpleNamespace(republish=rule)) == []


def test_a_republish_rule_without_headers_only_is_read_as_not_headers_only():
    rule = SimpleNamespace(src=">", dest="m.>")

    assert bucket_drift(DECLARED_REPUBLISH, SimpleNamespace(republish=rule)) == []


@pytest.mark.parametrize(
    "placement",
    [SimpleNamespace(cluster=5, tags=None), SimpleNamespace(cluster="c", tags=5)],
    ids=["cluster-is-not-text", "tags-are-not-a-list"],
)
def test_a_placement_not_shaped_like_one_is_not_drift(placement):
    declared = BucketConfig(name="b", placement=Placement(cluster="c"))

    assert bucket_drift(declared, SimpleNamespace(placement=placement)) == []


def test_a_placement_of_tags_alone_is_compared():
    declared = BucketConfig(name="b", placement=Placement(tags=["a"]))

    drift = bucket_drift(declared, SimpleNamespace(placement=Placement(tags=["b"])))

    assert [option for option, _, _ in drift] == ["placement"]


def test_a_placement_of_a_cluster_alone_is_compared():
    declared = BucketConfig(name="b", placement=Placement(cluster="a"))

    drift = bucket_drift(declared, SimpleNamespace(placement=Placement(cluster="b")))

    assert [option for option, _, _ in drift] == ["placement"]


def test_a_description_that_is_not_text_is_not_drift():
    declared = BucketConfig(name="b", description="d")

    assert bucket_drift(declared, SimpleNamespace(description=5)) == []


def test_an_allow_direct_that_is_not_a_bool_is_not_drift():
    declared = BucketConfig(name="b", direct=True)

    assert bucket_drift(declared, SimpleNamespace(allow_direct="yes")) == []
