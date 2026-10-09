"""The example in `docs/service-templates.md` runs and builds what the page says it builds.

`test_docs_code_blocks_resolve` proves the fence parses and its imports exist.
It does not run it, so an example that registered and then failed to construct
would pass. This runs the fence and reads the child it leaves behind.
"""

import pytest

from tests.repo.test_docs_code_blocks_resolve import REPO, fences

pytestmark = pytest.mark.repo

DOC = REPO / "docs" / "service-templates.md"


def run_example(source: str) -> dict:
    namespace = {"__name__": "service_templates_example"}
    exec(compile(source, str(DOC), "exec"), namespace)
    return namespace


def test_the_registration_example_builds_an_unstarted_child_at_the_assigned_runtime():
    found = fences(DOC)
    assert found, "the document has no python fence"
    namespace = run_example(found[0][1])
    child = namespace["child"]
    assert type(child) is namespace["Shipments"]
    assert child.warehouse == "north"
    assert child.config.name == "shipment_batch_a"
    assert child.config.namespace == "retail"
    assert child.config.max_rpc_concurrency == 8
    assert child.config.max_async_rpc_concurrency == 8
    assert child.nc is None
    assert not child.container.lifecycle.is_running
    registered = namespace["template"]
    assert namespace["catalog"].resolve("shipments", "warehouse-a") is registered
    assert registered.definition.startup_timeout == 10
    assert registered.definition.cleanup_timeout == 5
