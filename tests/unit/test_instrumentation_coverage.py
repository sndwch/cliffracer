"""Tests verifying NATS message callbacks dispatch through the hook chain."""

import ast
import inspect
import re
from pathlib import Path

import pytest

from cliffracer.core import dispatcher as dispatcher_module
from cliffracer.core.container import Container

# Match callback argument bindings on nats-py calls.
_CALLBACK_BINDING = re.compile(r"\b(\w*cb)\s*=\s*self\.((?:dispatcher\.)?\w+)")

# Expected count of callback entry points.
_EXPECTED_CALLBACK_COUNT = 5


def _nats_callback_bindings(source: str | None = None) -> list[tuple[str, str]]:
    """(kwarg, target) for every callback handed to nats-py across all Container methods."""
    if source is None:
        source = inspect.getsource(Container)
    return _CALLBACK_BINDING.findall(source)


def _callbacks_bound_to_nats(source: str | None = None) -> set[str]:
    return {
        target.split(".")[-1] for kwarg, target in _nats_callback_bindings(source) if kwarg == "cb"
    }


def _dispatcher_methods() -> dict[str, ast.AST]:
    dispatcher_file = Path(inspect.getfile(dispatcher_module))
    files = [dispatcher_file]
    dispatch_dir = dispatcher_file.parent / "dispatch"
    if dispatch_dir.is_dir():
        files.extend(sorted(dispatch_dir.glob("*.py")))

    methods: dict[str, ast.AST] = {}
    for path in files:
        tree = ast.parse(path.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                for item in n.body:
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                        methods[item.name] = item
    return methods


def _reaches(name: str, target: str, methods: dict[str, ast.AST], seen: set[str]) -> bool:
    """Does `name` reach `target` through calls to other MessageDispatcher methods?

    Transitive on purpose: entrypoint callbacks delegate through internal dispatch
    methods that call _run_worker -- so this transitive walk ensures every NATS
    entrypoint executes through the worker hook pipeline.
    """
    if name in seen or name not in methods:
        return False
    seen.add(name)
    for node in ast.walk(methods[name]):
        if isinstance(node, ast.Attribute):
            if node.attr == target:
                return True
            if node.attr in methods and _reaches(node.attr, target, methods, seen):
                return True
    return False


@pytest.mark.unit
def test_the_callback_scan_still_finds_callbacks():
    """Guard the guard: prove the extraction works before trusting it."""
    found = _callbacks_bound_to_nats()

    assert len(found) == _EXPECTED_CALLBACK_COUNT, (
        f"expected {_EXPECTED_CALLBACK_COUNT} 'cb=self.<name>' bindings in "
        f"_setup_subscriptions, found {sorted(found)}. If a callback was added or "
        "removed deliberately, update _EXPECTED_CALLBACK_COUNT; if this dropped to "
        "zero, the regex stopped matching and every other test here is now vacuous."
    )


@pytest.mark.unit
def test_the_reachability_scan_can_tell_reached_from_unreached():
    """Guard the guard, second half: a walk that returns True for everything --
    or False for everything -- satisfies the assertion below either way."""
    methods = _dispatcher_methods()
    assert "_run_worker" in methods, "the target itself is gone; this file is now vacuous"

    assert _reaches("handle_event", "_run_worker", methods, set()), (
        "positive control: handle_event is known to dispatch through the chain"
    )
    # NEGATIVE control, and it must be a REAL method rather than an invented
    # name: a missing name returns False from the first line of _reaches, which
    # would pass this assertion without the walk doing anything at all.
    assert "report_consumer_drift" in methods
    assert not _reaches("report_consumer_drift", "_run_worker", methods, set()), (
        "negative control: MessageDispatcher.report_consumer_drift is not a dispatch path, so a scan "
        "that reports it as reaching _run_worker is reporting True for everything"
    )


@pytest.mark.unit
def test_every_nats_callback_dispatches_through_the_hook_chain():
    methods = _dispatcher_methods()
    uninstrumented = sorted(
        cb for cb in _callbacks_bound_to_nats() if not _reaches(cb, "_run_worker", methods, set())
    )

    assert not uninstrumented, (
        f"{uninstrumented} are handed to NATS as callbacks but never reach MessageDispatcher._run_worker."
    )


@pytest.mark.unit
def test_every_nats_callback_binds_to_dispatcher():
    """Verify entry points handed to nats-py resolve directly to dispatcher instance."""
    bindings = _nats_callback_bindings()

    assert len(bindings) == _EXPECTED_CALLBACK_COUNT, (
        f"expected {_EXPECTED_CALLBACK_COUNT} nats-py callback bindings across Container methods, "
        f"found {bindings}. If this dropped, the regex "
        "stopped matching and this test is now vacuous."
    )

    not_on_dispatcher = [(k, t) for k, t in bindings if not t.startswith("dispatcher.")]
    assert not not_on_dispatcher, (
        f"these callbacks do not bind directly to dispatcher: {not_on_dispatcher}. "
        "Callbacks must bind directly to self.dispatcher.<method> to avoid service bounce."
    )


@pytest.mark.unit
def test_callback_scan_finds_callbacks_in_methods_other_than_setup_and_connect():
    """Verify callback scan inspects helper methods beyond setup and connect."""
    helper_source = (
        "class ContainerWithHelper:\n"
        "    async def _setup_control_subscription(self):\n"
        "        await self.nc.subscribe('control.>', cb=self.dispatcher._handle_raw_event)\n"
        "    async def _handle_raw_event(self, msg):\n"
        "        pass\n"
    )
    bindings = _nats_callback_bindings(helper_source)
    assert ("cb", "dispatcher._handle_raw_event") in bindings

    cb_targets = _callbacks_bound_to_nats(helper_source)
    assert "_handle_raw_event" in cb_targets
