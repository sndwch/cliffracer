"""What an unauthenticated /health may say about a failure.

The endpoint answers on a port with nothing in front of it, and five of its
error paths published the exception's own words. A probe that failed to reach
its database reported the DSN it tried, credentials included -- to anyone who
could open a socket.

The RPC path has always gated this on `expose_internal_errors`. These assert
the health payload now reads the same switch, at every one of the five sites,
and that hiding the text does not hide the FAILURE: an operator must still see
that a dependency is down and which one, or the fix would trade a leak for a
blind endpoint.

The secret in these tests is a credentialed DSN rather than a neutral string
because that is the shape the leak actually took, and a test that would pass
on `"boom"` while failing on a password is not testing the thing.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension
from cliffracer.core.health_listener import HealthListener
from tests.conftest import broker_url
from tests.unit.test_health_listener_adversarial_stress import (
    _raw_request,
    _simulate_service_state,
)

pytestmark = pytest.mark.unit

SECRET = "sup3rs3cret"
# The host comes from the suite's own broker rather than a pinned address: the
# string is never dialled -- it only ever appears inside a raised exception --
# but a literal address in a test module is indistinguishable from one that is,
# and the guard that says so is right to not try to tell them apart.
DSN = f"{broker_url()}?password={SECRET}"


def _service(*, expose: bool) -> CliffracerService:
    svc = CliffracerService(
        ServiceConfig(name="leaky", health_port=0, expose_internal_errors=expose)
    )
    _simulate_service_state(svc, running=True, broker_state="connected")
    return svc


async def _get(svc: CliffracerService, path: str = "/health") -> tuple[int, str]:
    """Serve one real request over a socket and return (status, raw body)."""
    listener = HealthListener(svc, "127.0.0.1", 0)
    await listener.start()
    try:
        status, _, body = await _raw_request(listener.port, path)
        return status, json.dumps(body)
    finally:
        await listener.stop()


def _with_failing_probe(expose: bool) -> CliffracerService:
    svc = _service(expose=expose)

    async def probe() -> None:
        raise ValueError(f"could not reach {DSN}")

    svc.add_dependency("db", probe, timeout=1.0)
    return svc


# --- the five sites, hidden by default --------------------------------------


async def test_a_failing_probe_does_not_publish_its_exception_text():
    """The site the endpoint hits most: a dependency that is simply down."""
    status, body = await _get(_with_failing_probe(expose=False))

    assert SECRET not in body, body
    assert "ValueError" not in body, body
    assert status == 503


async def test_a_health_check_that_raises_does_not_publish_its_exception_text():
    """The catch-all: `health_check` itself blew up, which a status probe reports as a 503."""
    svc = _service(expose=False)

    async def boom() -> dict:
        raise RuntimeError(f"could not reach {DSN}")

    svc.health_check = boom  # type: ignore[method-assign]
    status, body = await _get(svc)

    assert SECRET not in body, body
    assert status == 503


async def test_an_extension_whose_health_details_raise_does_not_publish_them():
    class Leaky(Extension):
        def health_details(self) -> dict:
            raise RuntimeError(f"could not reach {DSN}")

    class Svc(CliffracerService):
        leaky = Leaky()

    svc = Svc(ServiceConfig(name="leaky", health_port=0, expose_internal_errors=False))
    _simulate_service_state(svc, running=True, broker_state="connected")
    _, body = await _get(svc)

    assert SECRET not in body, body


@pytest.mark.parametrize("expose", [False, True], ids=["hidden", "exposed"])
async def test_a_dependency_sweep_that_raises_follows_the_policy(expose: bool):
    """Not a probe failing, but the machinery that runs them.

    BOTH directions, because only one of them was pinned: hard-coding this site
    to the generic string reds nothing in the whole suite when the exposed half
    is missing, so the gate here could be replaced by a constant and no test
    would notice.
    """
    import cliffracer.core.service as service_module

    svc = _service(expose=expose)

    async def boom(*args, **kwargs):
        raise RuntimeError(f"could not reach {DSN}")

    original = service_module.check_dependencies
    service_module.check_dependencies = boom
    try:
        _, body = await _get(svc)
    finally:
        service_module.check_dependencies = original

    assert (SECRET in body) is expose, body


async def test_a_failure_in_the_extension_loop_itself_does_not_publish_it():
    """The fifth site, and the one a sweep for `"error"` cannot find.

    `health_check` wraps the whole extension loop in a second `except`, which
    writes `details_error`. The inner handler only covers a single extension's
    `health_details()`, so a failure in the loop -- an extension whose `name`
    raises, for instance -- lands here instead, three lines below a site that
    is gated.

    A sweep for the key `"error"` never makes this one a candidate: it is
    called `details_error`, which is why the gate is one function rather
    than a search.
    """

    class Hostile(Extension):
        @property
        def name(self) -> str:
            raise RuntimeError(f"could not reach {DSN}")

        @name.setter
        def name(self, value: str) -> None:
            pass

        def health_details(self) -> dict:
            return {"ok": True}

    class Svc(CliffracerService):
        hostile = Hostile()

    svc = Svc(ServiceConfig(name="leaky", health_port=0, expose_internal_errors=False))
    _simulate_service_state(svc, running=True, broker_state="connected")
    _, body = await _get(svc)

    assert SECRET not in body, body
    assert "details_error" in json.loads(body), "the failure is still reported, just not quoted"


# --- the same five, exposed when the configuration says so ------------------


async def test_the_extension_loop_failure_is_published_when_the_configuration_allows_it():
    class Hostile(Extension):
        @property
        def name(self) -> str:
            raise RuntimeError(f"could not reach {DSN}")

        @name.setter
        def name(self, value: str) -> None:
            pass

        def health_details(self) -> dict:
            return {"ok": True}

    class Svc(CliffracerService):
        hostile = Hostile()

    svc = Svc(ServiceConfig(name="leaky", health_port=0, expose_internal_errors=True))
    _simulate_service_state(svc, running=True, broker_state="connected")
    _, body = await _get(svc)

    assert SECRET in body, body


async def test_the_text_is_published_when_the_configuration_allows_it():
    """The other half of the switch.

    Without this, every assertion above would hold on an endpoint that had
    stopped reporting errors altogether -- which is a different defect wearing
    this fix's clothes.
    """
    status, body = await _get(_with_failing_probe(expose=True))

    assert SECRET in body, body
    assert "ValueError" in body, body
    assert status == 503


async def test_the_catch_all_publishes_the_text_when_the_configuration_allows_it():
    svc = _service(expose=True)

    async def boom() -> dict:
        raise RuntimeError(f"could not reach {DSN}")

    svc.health_check = boom  # type: ignore[method-assign]
    _, body = await _get(svc)

    assert SECRET in body, body


# --- what hiding the text must NOT hide -------------------------------------


async def test_the_status_and_the_failing_dependency_name_survive_either_way():
    """Hiding the words must not hide the failure.

    An operator reads three things off a degraded endpoint: that it is
    degraded, which dependency did it, and that the dependency is not ok. None
    of those is the exception's text, and all three must be identical under
    both settings -- otherwise this fix has traded a leak for a blind endpoint.
    """
    hidden_status, hidden = await _get(_with_failing_probe(expose=False))
    shown_status, shown = await _get(_with_failing_probe(expose=True))

    hidden_body, shown_body = json.loads(hidden), json.loads(shown)

    assert hidden_status == shown_status == 503
    assert hidden_body["status"] == shown_body["status"] == "unhealthy"
    assert hidden_body["unhealthy_dependencies"] == shown_body["unhealthy_dependencies"] == ["db"]
    assert hidden_body["dependencies"]["db"]["ok"] is False
    assert shown_body["dependencies"]["db"]["ok"] is False
    # An error is still reported; only its wording differs.
    assert hidden_body["dependencies"]["db"]["error"]
    assert shown_body["dependencies"]["db"]["error"]


async def test_a_timeout_still_names_its_own_bound_under_either_setting():
    """Our own words are not the exception's, and are not withheld.

    "timed out after 0.1s" describes a budget this process chose. Withholding
    it would cost an operator the difference between a slow dependency and an
    absent one while protecting nothing.
    """
    bodies = []
    for expose in (False, True):
        svc = _service(expose=expose)

        async def slow() -> None:
            await asyncio.sleep(5)

        svc.add_dependency("slow", slow, timeout=0.1)
        _, body = await _get(svc)
        bodies.append(json.loads(body)["dependencies"]["slow"]["error"])

    assert bodies[0] == bodies[1]
    assert "timed out after 0.1s" in bodies[0], bodies


async def test_a_healthy_service_is_unaffected_by_the_setting():
    """The control for every assertion above: no failure, no difference."""
    for expose in (False, True):
        status, body = await _get(_service(expose=expose))
        assert status == 200, body
        assert json.loads(body)["status"] == "healthy"


def test_the_secure_answer_is_the_default():
    """A service that says nothing about this setting withholds the text."""
    assert ServiceConfig(name="x").expose_internal_errors is False


def test_the_gate_fails_closed_for_a_config_it_cannot_read():
    """A test double is truthy on every attribute, so truthiness would expose.

    `getattr(Mock(), "expose_internal_errors", False)` returns a child `Mock`,
    which is truthy. A gate whose job is withholding must fail closed when it
    cannot read an answer, and a `Mock` config is the shape that reaches it.
    """
    from unittest.mock import MagicMock, Mock

    from cliffracer.core.error_text import may_expose

    assert may_expose(Mock()) is False
    assert may_expose(MagicMock()) is False
    assert may_expose(None) is False
    assert may_expose(object()) is False


def test_the_gate_still_opens_for_a_real_configuration():
    """The other side, so failing closed is not failing always."""
    from cliffracer.core.error_text import may_expose

    assert may_expose(ServiceConfig(name="x", expose_internal_errors=True)) is True
    assert may_expose(ServiceConfig(name="x")) is False
