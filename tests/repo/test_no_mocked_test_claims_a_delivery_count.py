"""A test standing in for the transport does not assert how often it delivered.

A mock does not redeliver. Whatever `num_delivered` a mocked message carries is
a value the test assigned to it, so reading one inside an `assert` is either
circular -- checking back the input -- or a claim about a broker that never ran.
Only a broker decides that a message came again.

**Assigning one is fine and must stay fine.** Driving the retry policy by
setting a delivery attempt is how the unit tier exercises it:

    msg.metadata.num_delivered = 1                 # an input
    MockJetStreamMsg(subject="x", num_delivered=5)  # an input

so the discriminator is the syntactic position, not the name. Only a read
inside an `ast.Assert` is reported. A keyword argument is an `ast.keyword` and
never an `ast.Attribute`, so the second shape is out of reach of the sweep by
construction rather than by exemption.

Scope is every test module and conftest under `tests/` and `packages/*/tests/`,
excluding `tests/integration/` and any module declaring `nats_required` -- those
have a broker and may say what it did.

**This is one pattern, not four.** Three other families of assertion were
counted against the tree and are deliberately not checked: asserting `ack`/`nak`/
`term` state is a claim about our own dispatcher (and about `MockMessage`
itself, which is the subject under test in one module); every
`pytest.raises(TimeoutError)` outside the integration tier is a mock
deliberately configured to raise with the test asserting propagation; and
`TestResponse.success` reports False for an unanswered message, so asserting it
is backed. The JetStream family waits on the harness driving JetStream at all.
The counts are in the pull request that added this file.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

# How many times a broker delivered a message. Nothing else owns these.
DELIVERY_ATTRS = frozenset({"num_delivered", "redelivered", "delivered"})

# Constructing one of these is what marks a module as standing in for the
# transport rather than talking to it.
MOCK_CONSTRUCTORS = frozenset(
    {
        "MockMessage",
        "ServiceTestHarness",
        "MockJetStreamContext",
        "AsyncMock",
        "MagicMock",
    }
)

# The reading when this guard was written -- this module included, since it
# sweeps itself. A sweep that collapses reports zero offenders exactly as a
# clean tree does, so the failure names these rather than leaving a smaller
# sweep to look like success.
MODULES_WHEN_WRITTEN = 239
MODULES_WITH_A_MOCK_WHEN_WRITTEN = 79


def suite_modules() -> list[Path]:
    """Every test module and conftest under the two test roots."""
    found = list((REPO / "tests").rglob("test_*.py"))
    found += list((REPO / "tests").rglob("conftest.py"))
    found += [p for p in (REPO / "packages").rglob("test_*.py") if "tests" in p.parts]
    found += [p for p in (REPO / "packages").rglob("conftest.py") if "tests" in p.parts]
    return sorted({p for p in found if "__pycache__" not in p.parts})


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return None


def declares_nats_required(tree: ast.Module) -> bool:
    """Whether the module carries the marker that says it dials a broker."""
    return any(
        isinstance(node, ast.Attribute) and node.attr == "nats_required" for node in ast.walk(tree)
    )


def constructs_a_mock(tree: ast.Module) -> set[str]:
    """Which transport stand-ins this module builds."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in MOCK_CONSTRUCTORS:
                found.add(name)
    return found


def delivery_reads_in_asserts(tree: ast.Module) -> list[tuple[int, str]]:
    """Every delivery attribute read inside an `assert`, with its line."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute) and inner.attr in DELIVERY_ATTRS:
                found.append((node.lineno, inner.attr))
    return sorted(set(found))


def offenders(paths: list[Path] | None = None) -> list[str]:
    """Modules claiming a delivery count without a broker to produce one."""
    found = []
    for path in paths if paths is not None else suite_modules():
        label = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)
        if "tests/integration" in label:
            continue
        tree = _parse(path)
        if tree is None or declares_nats_required(tree):
            continue
        mocks = constructs_a_mock(tree)
        if not mocks:
            continue
        for lineno, attr in delivery_reads_in_asserts(tree):
            found.append(f"{label}:{lineno} asserts {attr} with {sorted(mocks)}")
    return found


def sweep_reading(paths: list[Path] | None = None) -> tuple[int, int]:
    """(modules walked, modules constructing a mock) -- the positive reading."""
    modules = paths if paths is not None else suite_modules()
    with_mock = 0
    for path in modules:
        tree = _parse(path)
        if tree is not None and constructs_a_mock(tree):
            with_mock += 1
    return len(modules), with_mock


def test_no_mocked_module_asserts_a_delivery_count():
    found = offenders()
    assert not found, (
        "a delivery count is what a broker decided, and these modules assert one "
        "while standing in for the transport. Set the attempt as an input and "
        "assert on what the code did with it, or move the test to "
        "tests/integration and mark it nats_required:\n  " + "\n  ".join(found)
    )


def test_the_sweep_walked_the_suite_and_saw_the_mocks():
    """No offenders and no modules read are the same output.

    The counts are named so a collapse in the sweep is a failure with a number
    rather than a quieter pass.
    """
    walked, with_mock = sweep_reading()
    assert walked > 0 and with_mock > 0, (
        f"the sweep walked {walked} modules and found {with_mock} constructing a "
        f"transport stand-in; when this guard was written the reading was "
        f"{MODULES_WHEN_WRITTEN} and {MODULES_WITH_A_MOCK_WHEN_WRITTEN}. A zero "
        "here means the sweep stopped reading, not that the suite is clean."
    )


def _write(tmp_path: Path, body: str, name: str = "test_sample.py") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def test_CONTROL_a_delivery_count_inside_an_assert_is_caught(tmp_path: Path):
    """The offence: a mocked module claiming the message came again."""
    path = _write(
        tmp_path,
        "from unittest.mock import AsyncMock\n"
        "\n"
        "async def test_redelivery():\n"
        "    msg = AsyncMock()\n"
        "    assert msg.metadata.num_delivered == 2\n",
    )
    found = offenders([path])
    assert found, "a delivery count asserted under a mock was not caught"
    assert "num_delivered" in found[0], found


def test_CONTROL_assigning_a_delivery_count_is_not_caught(tmp_path: Path):
    """Setting the attempt is how the retry policy is driven."""
    path = _write(
        tmp_path,
        "from unittest.mock import AsyncMock\n"
        "\n"
        "async def test_policy():\n"
        "    msg = AsyncMock()\n"
        "    msg.metadata.num_delivered = 1\n"
        "    assert msg.nak.await_count == 0\n",
    )
    assert offenders([path]) == [], "an assigned delivery count was reported"


def test_CONTROL_a_delivery_count_as_a_keyword_argument_is_not_caught(tmp_path: Path):
    """The other input shape, which the suite uses a dozen times."""
    path = _write(
        tmp_path,
        "from unittest.mock import AsyncMock\n"
        "\n"
        "def _msg(num_delivered=1):\n"
        "    return AsyncMock()\n"
        "\n"
        "async def test_backoff():\n"
        "    msg = _msg(num_delivered=5)\n"
        "    assert msg.nak.await_count == 0\n",
    )
    assert offenders([path]) == [], "a keyword-argument delivery count was reported"


def test_CONTROL_dispatch_logic_with_the_same_mocks_is_not_caught(tmp_path: Path):
    """The same stand-ins asserting what our own code did, which is backed."""
    path = _write(
        tmp_path,
        "from cliffracer.testing import MockMessage\n"
        "\n"
        "async def test_the_dispatcher_acknowledges():\n"
        "    msg = MockMessage(subject='x.y')\n"
        "    await msg.ack()\n"
        "    assert msg.acked is True\n"
        "    assert msg.nak_delay is None\n",
    )
    assert offenders([path]) == [], "an assertion about our own dispatch was reported"


def test_CONTROL_a_module_with_no_mock_is_not_caught(tmp_path: Path):
    """Without a stand-in there is nothing to claim a broker's behaviour against."""
    path = _write(
        tmp_path,
        "async def test_live(js):\n    msg = await js.next_msg()\n"
        "    assert msg.metadata.num_delivered == 2\n",
    )
    assert offenders([path]) == [], "a module with no transport stand-in was reported"


def test_CONTROL_the_marker_and_the_integration_tier_are_both_respected(tmp_path: Path):
    """A test that says it has a broker may say what the broker did."""
    offending = (
        "from unittest.mock import AsyncMock\n"
        "\n"
        "async def test_redelivery():\n"
        "    msg = AsyncMock()\n"
        "    assert msg.metadata.num_delivered == 2\n"
    )
    marked = _write(
        tmp_path,
        "import pytest\n\npytestmark = pytest.mark.nats_required\n\n" + offending,
        name="test_marked.py",
    )
    assert offenders([marked]) == [], "a nats_required module was reported"

    integration = tmp_path / "tests" / "integration"
    integration.mkdir(parents=True)
    in_tier = integration / "test_live.py"
    in_tier.write_text(offending)
    assert offenders([in_tier]) == [], "a tests/integration module was reported"
