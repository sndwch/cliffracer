"""The generator describes a service through a client role the broker confines to an inbox prefix.

Against a broker enforcing the roles `broker_permissions` derives: the customer role may publish
the describe endpoint and subscribe only to its own `_INBOX.customer.>`. The command now names that
prefix with `--inbox-prefix`, and says what the broker refused when it is missing or the
password is wrong, where it reported a service that did not answer and a broker that is not there.
"""

import asyncio

import pytest

from cliffracer import ServiceConfig
from cliffracer.broker_permissions import broker_permissions
from cliffracer.generate_client.cli import main
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


async def _generate(capsys, url: str, *extra: str, out) -> tuple[int, str]:
    argv = [
        "--service",
        "orders",
        "--namespace",
        "retail",
        "--nats-url",
        url,
        "--timeout",
        "3",
        "--out",
        str(out),
        *extra,
    ]
    rc = await asyncio.to_thread(main, argv)
    return rc, capsys.readouterr().err


async def test_inbox_prefix_lets_a_confined_role_describe_and_a_missing_one_is_named(
    secured_broker, monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "east")
    start, _connect, serve, _errors = secured_broker
    cfg = config()
    service_policy = broker_permissions(Orders, cfg, role="service")
    client_policy = broker_permissions(Orders, cfg, role="client", inbox_prefix="_INBOX.customer")
    url, _inspector = await start({"warehouse": service_policy, "customer": client_policy})
    await serve(Orders, cfg, url, "warehouse")
    as_customer = url.replace("nats://", "nats://customer:disposable@")

    rc, err = await _generate(
        capsys, as_customer, "--inbox-prefix", "_INBOX.customer", out=tmp_path / "ok.py"
    )
    assert (rc, err) == (0, ""), err
    assert "class OrdersClient" in (tmp_path / "ok.py").read_text()

    rc, err = await _generate(capsys, as_customer, out=tmp_path / "no_prefix.py")
    assert rc == 3, err
    assert "refused this client a permission" in err and "--inbox-prefix" in err, err
    assert "no service" not in err
    assert not (tmp_path / "no_prefix.py").exists()

    wrong = url.replace("nats://", "nats://customer:WRONG@")
    rc, err = await _generate(
        capsys, wrong, "--inbox-prefix", "_INBOX.customer", out=tmp_path / "wrong.py"
    )
    assert rc == 3, err
    assert "refused this client's credentials" in err, err
    assert "no broker reachable" not in err and "WRONG" not in err
    assert not (tmp_path / "wrong.py").exists()
