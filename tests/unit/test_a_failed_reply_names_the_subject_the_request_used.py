"""A reply that is a failure without saying why is named for what it is, at the subject used.

`_raise_for_error` returns when the envelope has no `error`, so `{"success": false}` reached the
check that the key is present and was reported as carrying no success key, which it does. The
message also built the subject as `{service}.{method}`, which is neither the subject the request
went to (`{namespace}.{service}.rpc.{method}`) nor one a broker log would show.
"""

import json
from unittest.mock import AsyncMock

import pytest

from cliffracer.client import RpcServerError, ServiceClient

pytestmark = pytest.mark.unit


class _Reply:
    def __init__(self, body: dict) -> None:
        self.data = json.dumps(body).encode()
        self.headers = {"Content-Type": "application/json"}


def _client(body: dict, **kwargs) -> tuple[ServiceClient, AsyncMock]:
    nc = AsyncMock()
    nc.request = AsyncMock(return_value=_Reply(body))
    return ServiceClient(nc=nc, service="math_svc", verify=False, **kwargs), nc


async def _call(client: ServiceClient) -> None:
    await client._call("calculate", {"x": 1}, int)


@pytest.mark.asyncio
async def test_success_false_with_no_error_is_a_failure_that_gave_no_reason():
    client, _ = _client({"success": False, "result": None})

    with pytest.raises(RpcServerError) as raised:
        await _call(client)

    message = str(raised.value)
    assert "math_svc.rpc.calculate failed without saying why" in message, message
    assert "success=False" in message and "no success key" not in message, message


@pytest.mark.asyncio
async def test_a_reply_with_no_success_key_still_says_so_and_names_the_real_subject():
    client, _ = _client({"result": 42}, namespace="shop")

    with pytest.raises(RpcServerError) as raised:
        await _call(client)

    assert "reply from shop.math_svc.rpc.calculate carries no success key" in str(raised.value)


@pytest.mark.asyncio
async def test_a_success_that_is_not_true_or_false_is_shown_as_it_arrived():
    client, _ = _client({"success": "yes", "result": 1})

    with pytest.raises(RpcServerError, match=r"success='yes'"):
        await _call(client)


@pytest.mark.asyncio
async def test_CONTROL_a_success_reply_is_returned():
    client, _ = _client({"success": True, "result": 3})

    assert await client._call("calculate", {"x": 1}, int) == 3


@pytest.mark.asyncio
async def test_the_request_is_labelled_json():
    client, nc = _client({"success": True, "result": 3})

    await client._call("calculate", {"x": 1}, int)

    headers = nc.request.await_args.kwargs["headers"]
    assert headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_a_content_type_the_caller_set_is_not_overridden_or_duplicated():
    client, nc = _client({"success": True, "result": 3}, headers={"content-type": "text/plain"})

    await client._call("calculate", {"x": 1}, int)

    headers = nc.request.await_args.kwargs["headers"]
    assert {k: v for k, v in headers.items() if k.lower() == "content-type"} == {
        "content-type": "text/plain"
    }
