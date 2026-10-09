"""A generated client must reach the service it was generated from.

`ServiceConfig.subject_prefix` puts every subject a service subscribes to
under an outermost token. A client that builds `<service>.<tail>` without it
asks on a subject nothing is listening to, and the failure is a
`NoRespondersError` naming a subject that looks perfectly correct.

The client holds no `ServiceConfig` -- it is generated code a caller
constructs -- so it reads the same environment variable the config field
defaults from, which is what the `describe` CLI does.
"""

from __future__ import annotations

import pytest

from cliffracer.client import ServiceClient

pytestmark = pytest.mark.unit


def test_the_client_targets_the_prefixed_subject(monkeypatch):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "envx")
    client = ServiceClient(service="warehouse")
    assert client._subject("describe") == "envx.warehouse.describe"


def test_the_prefix_goes_outside_the_namespace(monkeypatch):
    """`<prefix>.<namespace>.<service>.<tail>`, the order the service subscribes in."""
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "envx")
    client = ServiceClient(service="warehouse", namespace="prod")
    assert client._subject("rpc.ship") == "envx.prod.warehouse.rpc.ship"


def test_CONTROL_without_a_prefix_the_subject_is_unchanged(monkeypatch):
    """So the tests above fail for the prefix, not for some other rewrite."""
    monkeypatch.delenv("CLIFFRACER_SUBJECT_PREFIX", raising=False)
    client = ServiceClient(service="warehouse")
    assert client._subject("describe") == "warehouse.describe"


def test_an_empty_prefix_is_not_a_prefix(monkeypatch):
    """An unset variable and one set to empty must read the same."""
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "")
    client = ServiceClient(service="warehouse")
    assert client._subject("describe") == "warehouse.describe"
