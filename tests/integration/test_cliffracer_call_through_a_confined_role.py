"""`cliffracer call` through a client role the broker confines, against a broker enforcing it.

The roles are the ones `broker_permissions` derives: the customer may publish the describe and RPC
subjects of `orders` and subscribe only to its own `_INBOX.customer.>`. With `--inbox-prefix` the
call is answered. Without it the broker refuses the reply subscription, and the command says the
broker refused a permission, not that nothing answered; with a wrong password it says the
credentials were refused, without printing them.
"""

import io
import json

import pytest

from cliffracer import ServiceConfig
from cliffracer.broker_permissions import broker_permissions
from cliffracer.cli.main import build_parser
from cliffracer.cli.operate import EXIT_NO_BROKER, run_async
from tests.fixtures.permission_orders import Orders

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


def config() -> ServiceConfig:
    return ServiceConfig(
        name="orders",
        namespace="retail",
        subject_prefix="east",
        nats_inbox_prefix="_INBOX.order_workers",
        health_port=0,
    )


async def _call(url: str, password: str, *extra: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    argv = [
        "call",
        "orders.reserve",
        "--arg",
        "quantity=2",
        "--namespace",
        "retail",
        "--server",
        url,
        "--user",
        "customer",
        "--password",
        password,
        "--timeout",
        "3",
        *extra,
    ]
    code = await run_async(build_parser().parse_args(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


async def test_a_confined_role_calls_with_its_inbox_prefix_and_is_told_what_is_refused(
    secured_broker, monkeypatch
):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "east")
    start, _connect, serve, _errors = secured_broker
    cfg = config()
    service_policy = broker_permissions(Orders, cfg, role="service")
    client_policy = broker_permissions(Orders, cfg, role="client", inbox_prefix="_INBOX.customer")
    url, _inspector = await start({"warehouse": service_policy, "customer": client_policy})
    await serve(Orders, cfg, url, "warehouse")

    code, out, err = await _call(url, "disposable", "--inbox-prefix", "_INBOX.customer")
    assert (code, err) == (0, ""), err
    assert json.loads(out) == 2

    code, out, err = await _call(url, "disposable")
    assert (code, out) == (EXIT_NO_BROKER, ""), err
    assert "refused this client a permission" in err and "--inbox-prefix" in err, err
    assert "nothing answered" not in err

    code, out, err = await _call(url, "WRONG", "--inbox-prefix", "_INBOX.customer")
    assert (code, out) == (EXIT_NO_BROKER, ""), err
    assert "refused this client's credentials" in err, err
    assert "WRONG" not in err
