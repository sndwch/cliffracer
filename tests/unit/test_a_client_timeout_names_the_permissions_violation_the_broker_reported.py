"""A `ServiceClient` timeout says why when the broker refused a permission while the request waited.

A role the broker confines to an inbox prefix cannot subscribe to the client's reply inbox. The
broker reports the violation to the connection's error callback and says nothing to the request,
which then times out, so the caller saw `orders.rpc.ping did not answer within 3s` and had no way
to tell a refused role from a service that is down. The generator already names the refusal. The
client keeps the permissions violations its connection reports and, when a request times out, adds
every one reported during that request's wait, oldest first, naming the subject the broker gave.

The attribution is per request. A violation reported before the request began is not its own. A
publish violation belongs to the request whose subject it names; a subscription violation belongs
to every request waiting, since the replies to all of them come back on one inbox subscription. The
exception stays `RpcTimeoutError`, so a handler of it still matches.
"""

import asyncio

import pytest
from nats import errors as nats_errors

from cliffracer.client import ServiceClient
from cliffracer.core import dial
from cliffracer.core.exceptions import RpcTimeoutError

pytestmark = pytest.mark.unit

INBOX = 'nats: permissions violation for subscription to "_inbox.abc123.*"'


def violation(kind: str, subject: str) -> nats_errors.Error:
    return nats_errors.Error(f'nats: permissions violation for {kind} to "{subject}"')


class Broker:
    """A connection whose requests wait, are told about a violation, then time out.

    `reports[subject]` is the error, or the list of errors in order, the broker reports to the
    error callback while that subject's request waits; a subject with none times out in silence.
    """

    def __init__(self, reports: dict[str, nats_errors.Error], error_cb) -> None:
        self.reports = reports
        self.error_cb = error_cb
        self.is_closed = False

    async def request(self, subject, payload, timeout=None, headers=None):
        await asyncio.sleep(0.02)
        reported = self.reports.get(subject, [])
        for error in reported if isinstance(reported, list) else [reported]:
            await self.error_cb(error)
        await asyncio.sleep(0.05)
        raise nats_errors.TimeoutError()

    async def close(self):
        self.is_closed = True


@pytest.fixture
def connect(monkeypatch):
    """Dial a `Broker`; the test sets what it reports and may report in between requests."""
    state: dict = {"reports": {}, "cb": None}

    async def fake(url, *, timeout, **options):
        state["cb"] = options["error_cb"]
        return Broker(state["reports"], options["error_cb"])

    monkeypatch.setattr(dial, "connect", fake)
    return state


def make_client() -> ServiceClient:
    return ServiceClient(
        service="orders", nats_url="nats://broker.example", timeout=1, verify=False
    )


async def timeout_of(client: ServiceClient, subject: str) -> RpcTimeoutError:
    with pytest.raises(RpcTimeoutError) as caught:
        await client._request(subject, b"{}")
    return caught.value


async def test_a_subscription_violation_reported_during_the_wait_is_named_with_the_hint(connect):
    connect["reports"]["orders.rpc.ping"] = nats_errors.Error(INBOX)

    error = await timeout_of(make_client(), "orders.rpc.ping")

    text = str(error)
    assert text.startswith("orders.rpc.ping did not answer within 1s; the broker reported")
    assert 'permissions violation for subscription to "_inbox.abc123.*"' in text
    assert "inbox_prefix=" in text
    assert isinstance(error, TimeoutError), "a handler of the timeout still matches"


async def test_a_publish_violation_names_its_subject_and_gives_no_inbox_hint(connect):
    connect["reports"]["orders.rpc.ping"] = violation("publish", "orders.rpc.ping")

    text = str(await timeout_of(make_client(), "orders.rpc.ping"))

    assert 'permissions violation for publish to "orders.rpc.ping"' in text
    assert "inbox_prefix" not in text


@pytest.mark.parametrize(
    "report",
    [
        pytest.param(nats_errors.Error(INBOX), id="a-subscription-violation"),
        pytest.param(
            violation("publish", "orders.rpc.ping"), id="a-publish-violation-for-its-subject"
        ),
    ],
)
async def test_a_violation_reported_before_the_request_began_is_not_its_own(connect, report):
    client = make_client()
    connect["reports"]["orders.rpc.ping"] = report
    first = await timeout_of(client, "orders.rpc.ping")
    del connect["reports"]["orders.rpc.ping"]

    second = await timeout_of(client, "orders.rpc.ping")

    assert "the broker reported" in str(first)
    assert str(second) == "orders.rpc.ping did not answer within 1s"


async def test_a_violation_reported_during_the_wait_is_named_though_an_older_one_is_recorded(
    connect,
):
    client = make_client()
    connect["reports"]["orders.rpc.ping"] = nats_errors.Error(INBOX)
    await timeout_of(client, "orders.rpc.ping")
    connect["reports"]["orders.rpc.ping"] = violation("publish", "orders.rpc.ping")

    second = str(await timeout_of(client, "orders.rpc.ping"))

    assert 'permissions violation for publish to "orders.rpc.ping"' in second
    assert "_inbox" not in second and "inbox_prefix" not in second


async def test_every_violation_of_one_wait_is_named_oldest_first(connect):
    connect["reports"]["orders.rpc.ping"] = [
        violation("publish", "orders.rpc.ping"),
        nats_errors.Error(INBOX),
    ]

    text = str(await timeout_of(make_client(), "orders.rpc.ping"))

    assert text == (
        "orders.rpc.ping did not answer within 1s; the broker reported while it waited: "
        'permissions violation for publish to "orders.rpc.ping"; then '
        'permissions violation for subscription to "_inbox.abc123.*". '
        "A client role the broker confines to an inbox prefix needs inbox_prefix= naming it."
    )


async def test_CONTROL_a_violation_before_the_wait_is_left_out_of_the_ones_named(connect):
    client = make_client()
    connect["reports"]["orders.rpc.ping"] = violation("publish", "orders.rpc.ping")
    await timeout_of(client, "orders.rpc.ping")
    connect["reports"]["orders.rpc.ping"] = [
        nats_errors.Error(INBOX),
        nats_errors.Error('nats: permissions violation for subscription to "_inbox.def456.*"'),
    ]

    text = str(await timeout_of(client, "orders.rpc.ping"))

    assert text.startswith(
        "orders.rpc.ping did not answer within 1s; the broker reported while it waited: "
        'permissions violation for subscription to "_inbox.abc123.*"; then '
        'permissions violation for subscription to "_inbox.def456.*".'
    ), text
    assert "publish" not in text


async def test_two_concurrent_requests_only_the_refused_one_carries_the_reason(connect):
    connect["reports"]["billing.rpc.charge"] = violation("publish", "billing.rpc.charge")
    client = make_client()

    refused, fine = await asyncio.gather(
        timeout_of(client, "billing.rpc.charge"), timeout_of(client, "orders.rpc.ping")
    )

    assert 'permissions violation for publish to "billing.rpc.charge"' in str(refused)
    assert str(fine) == "orders.rpc.ping did not answer within 1s"


async def test_a_subscription_violation_is_every_waiting_requests_since_they_share_an_inbox(
    connect,
):
    connect["reports"]["billing.rpc.charge"] = nats_errors.Error(INBOX)
    client = make_client()

    first, second = await asyncio.gather(
        timeout_of(client, "billing.rpc.charge"), timeout_of(client, "orders.rpc.ping")
    )

    for error in (first, second):
        assert '"_inbox.abc123.*"' in str(error)


async def test_CONTROL_an_error_that_is_not_a_permissions_violation_adds_nothing(connect):
    connect["reports"]["orders.rpc.ping"] = nats_errors.Error("nats: 'Slow Consumer'")

    error = await timeout_of(make_client(), "orders.rpc.ping")

    assert str(error) == "orders.rpc.ping did not answer within 1s"


async def test_CONTROL_a_violation_names_its_subject_case_insensitively(connect):
    # The broker lower-cases the subject it names in some messages.
    connect["reports"]["Orders.rpc.Ping"] = violation("publish", "orders.rpc.ping")

    text = str(await timeout_of(make_client(), "Orders.rpc.Ping"))

    assert "permissions violation for publish" in text


async def test_CONTROL_a_borrowed_connection_keeps_its_error_callback_and_reports_nothing_extra():
    """The client hooks only a connection it dials; a borrowed one's violations are its owner's."""
    reported: list[str] = []

    async def owners_callback(error: Exception) -> None:
        reported.append(str(error))

    class Borrowed:
        is_closed = False

        def __init__(self) -> None:
            self._error_cb = owners_callback

        async def request(self, *args, **kwargs):
            await self._error_cb(nats_errors.Error(INBOX))
            raise nats_errors.TimeoutError()

    borrowed = Borrowed()
    client = ServiceClient(borrowed, service="orders", timeout=1, verify=False)  # type: ignore[arg-type]

    error = await timeout_of(client, "orders.rpc.ping")

    assert reported == [INBOX], "the broker reported the violation while the request waited"
    assert str(error) == "orders.rpc.ping did not answer within 1s"
    assert borrowed._error_cb is owners_callback
