"""The release job's prerelease tag is one the version backend can bump.

hatch-vcs derives every distribution's version from the nearest tag, and its
default scheme has to produce a version newer than that tag whenever the tree
is not exactly the tagged commit -- one commit later, or merely edited. It
refuses to do so when the tag's own version carries a non-zero ``dev``
component, because there is no defined successor to a ``.devN`` release:

    choosing custom numbers for the `.devX` distance is not supported

So a prerelease token that normalises to a ``.devN`` version makes every build
after a release fail: the packaging guards, a contributor's ``uv build``, and
the next release's own ``uv sync``. The token is the only thing that decides
this, and it lives in one line of one workflow, which is what this reads.
"""

import re
from pathlib import Path

import pytest
from packaging.version import Version

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = (REPO / ".gitea" / "workflows", REPO / ".github" / "workflows")

# The token as semantic-release is invoked with it.
TOKEN = re.compile(r"--prerelease-token\s+(?P<token>[A-Za-z0-9.]+)")


def prerelease_tokens() -> list[tuple[str, str]]:
    """Every (workflow path, prerelease token) the release jobs pass."""
    found = []
    for directory in WORKFLOWS:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.yml")):
            for match in TOKEN.finditer(path.read_text()):
                found.append((str(path.relative_to(REPO)), match.group("token")))
    return found


def unbumpable(tokens: list[tuple[str, str]]) -> list[str]:
    """Tokens whose prerelease version carries a dev component the backend cannot bump."""
    bad = []
    for where, token in tokens:
        # The shape semantic-release emits for a token is `v<version>-<token>.<n>`.
        version = Version(f"1.0.0-{token}.1")
        if version.dev is not None:
            bad.append(f"{where}: --prerelease-token {token} -> {version}, dev={version.dev}")
    return bad


@pytest.mark.gitea_checkout
def test_a_release_prerelease_token_yields_a_bumpable_version():
    """Every token a release job passes normalises to a version with no dev component."""
    tokens = prerelease_tokens()
    assert tokens, "no release job passes --prerelease-token, so this guard reads nothing"
    assert unbumpable(tokens) == []


def test_CONTROL_a_dev_token_is_caught():
    """The reader rejects the token that produced the failure."""
    assert unbumpable([("workflow.yml", "dev")])


def test_CONTROL_a_release_candidate_token_is_not_caught():
    """The reader accepts tokens that normalise to a prerelease with no dev component."""
    assert not unbumpable([("workflow.yml", "rc"), ("workflow.yml", "a"), ("workflow.yml", "b")])
