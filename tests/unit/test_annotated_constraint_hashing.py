"""Unit tests for Annotated validation constraint extraction and signature hashing."""

from typing import Annotated

import pytest
from pydantic import Field

from cliffracer import CliffracerService, rpc
from cliffracer.client import ClientOutOfDate, ServiceClient
from cliffracer.introspect import canonical, describe

pytestmark = pytest.mark.unit


def test_constraint_change_changes_signature_and_description_hash():
    """Changing a Field constraint (ge=1 -> ge=10) changes signature_hash and description_hash."""

    class ServiceV1(CliffracerService):
        @rpc
        async def submit(self, count: Annotated[int, Field(ge=1)]) -> int:
            return count

    class ServiceV2(CliffracerService):
        @rpc
        async def submit(self, count: Annotated[int, Field(ge=10)]) -> int:
            return count

    desc1 = describe(ServiceV1, service="sub_svc", version="1.0.0")
    desc2 = describe(ServiceV2, service="sub_svc", version="1.0.0")

    method1 = desc1.method("submit")
    method2 = desc2.method("submit")
    assert method1 is not None and method2 is not None

    # Verify constraints are present in TypeRef
    assert method1.params[0].type["constraints"] == {"ge": 1}
    assert method2.params[0].type["constraints"] == {"ge": 10}

    # Signature hashes must differ
    assert method1.signature_hash != method2.signature_hash

    # Overall description hashes must differ
    assert desc1.description_hash != desc2.description_hash


def test_string_constraints_extracted_and_hashed():
    """String constraints (min_length, max_length, pattern) are extracted into constraints dict."""

    class StringService(CliffracerService):
        @rpc
        async def set_code(
            self,
            code: Annotated[str, Field(min_length=3, max_length=10, pattern=r"^[A-Z]+$")],
        ) -> str:
            return code

    desc = describe(StringService, service="str_svc", version="1.0.0")
    method = desc.method("set_code")
    assert method is not None
    constraints = method.params[0].type["constraints"]
    assert constraints == {
        "min_length": 3,
        "max_length": 10,
        "pattern": r"^[A-Z]+$",
    }


@pytest.mark.asyncio
async def test_service_client_detects_constraint_drift():
    """ServiceClient.verify() raises ClientOutOfDate when server changes Field constraints."""

    class ServerV2(CliffracerService):
        @rpc
        async def process(self, value: Annotated[int, Field(ge=100)]) -> int:
            return value

    desc_server = describe(ServerV2, service="proc_svc", version="2.0.0")

    class FakeNC:
        async def request(self, subject: str, data: bytes, timeout: float = 5.0, headers=None):
            class FakeMsg:
                def __init__(self, d):
                    self.data = d
                    self.headers = None

            if subject.endswith(".describe"):
                return FakeMsg(canonical(desc_server.to_dict()).encode())
            return FakeMsg(b'{"success": true, "result": 100}')

    # Client built against V1 (where ge was 1, so signature_hash was different)
    class V1Client(ServiceClient):
        SIGNATURES = {
            "process": "sha256:stale_hash_from_v1",
        }

    client = V1Client(FakeNC(), service="proc_svc")
    with pytest.raises(ClientOutOfDate) as exc_info:
        await client.verify()

    assert "process" in exc_info.value.changed
