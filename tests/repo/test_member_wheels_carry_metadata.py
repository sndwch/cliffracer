"""Tests ensuring built wheels carry valid descriptions and licenses."""

import email
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.repo.built_distributions import build_all, uv_build

pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[2]


# Exact distribution set expected in builds.
EXPECTED_DISTRIBUTIONS = {
    "cliffracer",
    "cliffracer_auth",
    "cliffracer_cron",
    "cliffracer_cyanide",
    "cliffracer_dlq",
    "cliffracer_kv",
    "cliffracer_logging",
    "cliffracer_metrics",
    "cliffracer_otel",
    "cliffracer_resilience",
}

# EMPTY, and it stays that way. cliffracer-auth was exempted here because its
# README was written against behaviour auth-context changes, so it landed
# with that PR -- and the exemption's own control failed until this line was
# emptied, which is why the removal could not be forgotten. A name added back
# here needs the same treatment: a control that fails once the exemption stops
# being true.
NO_README_YET: set[str] = set()


def _declared_at(name: str) -> str:
    """`path:line` of a module-level assignment in this file.

    Computed rather than written down, so the instruction in a failure message
    cannot drift from where the list actually is.
    """
    path = Path(__file__)
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith(f"{name} ="):
            return f"{path.relative_to(ROOT)}:{number}"
    return str(path.relative_to(ROOT))  # pragma: no cover - the name always exists


def _build(out: Path) -> list[Path]:
    return build_all(ROOT, out)


def _wheels(out: Path) -> dict[str, zipfile.ZipFile]:
    return {f.name.split("-")[0]: zipfile.ZipFile(f) for f in _build(out) if f.suffix == ".whl"}


def _description(z: zipfile.ZipFile) -> str:
    name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
    payload = email.message_from_bytes(z.read(name)).get_payload()
    return str(payload or "").strip()


def _licence_entries(z: zipfile.ZipFile) -> list[str]:
    return [n for n in z.namelist() if "/licenses/" in n or n.endswith(".dist-info/LICENSE")]


@pytest.mark.slow
def test_every_wheel_carries_a_licence(tmp_path):
    wheels = _wheels(tmp_path / "dist")

    # THE ALLOWLIST IS THE POINT, so this is not derived from the workspace: a
    # distribution appearing that nobody declared is a thing worth failing on,
    # and comparing the workspace against itself would assert nothing. What the
    # failure owes the reader is where to go, which is why the location is in
    # the message -- adding a member means editing one named line.
    appeared = sorted(set(wheels) - EXPECTED_DISTRIBUTIONS)
    vanished = sorted(EXPECTED_DISTRIBUTIONS - set(wheels))
    assert not appeared, (
        f"the workspace builds {appeared}, which {_declared_at('EXPECTED_DISTRIBUTIONS')} "
        f"does not list. If that is a new member package, add it there; nothing else "
        f"about it is wrong. If it is not, a distribution is being built that nobody "
        f"declared."
    )
    assert not vanished, (
        f"{_declared_at('EXPECTED_DISTRIBUTIONS')} lists {vanished}, which the "
        f"workspace no longer builds. Remove them there, or find out why they "
        f"stopped building."
    )

    missing = sorted(n for n, z in wheels.items() if not _licence_entries(z))
    assert not missing, (
        f"{missing} declare MIT in their metadata and ship no LICENSE file. "
        'Add license-files = ["LICENSE"] and copy the file into the package.'
    )


def _blank_descriptions(wheels: dict[str, zipfile.ZipFile], exempt: set[str]) -> list[str]:
    return sorted(n for n, z in wheels.items() if not _description(z) and n not in exempt)


def test_CONTROL_an_exemption_exempts_only_what_it_names(tmp_path):
    """The filter, with a set that is not empty: NO_README_YET is empty and stays that way, so
    nothing else would notice the filter inverted."""

    def wheel(name: str, description: str) -> zipfile.ZipFile:
        path = tmp_path / f"{name}.whl"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(
                f"{name}-1.0.0.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0.0\n\n{description}",
            )
        return zipfile.ZipFile(path)

    wheels = {"a": wheel("a", ""), "b": wheel("b", ""), "c": wheel("c", "described")}

    assert _blank_descriptions(wheels, set()) == ["a", "b"]
    assert _blank_descriptions(wheels, {"a"}) == ["b"]


@pytest.mark.slow
def test_every_wheel_carries_a_description(tmp_path):
    wheels = _wheels(tmp_path / "dist")

    blank = _blank_descriptions(wheels, NO_README_YET)
    assert not blank, (
        f"{blank} have a zero-byte description, so their registry page and "
        '`pip show` are blank. Declare readme = "README.md" in the member\'s '
        "pyproject; hatchling sweeping the file into the sdist is not the same "
        "thing as declaring it."
    )


@pytest.mark.slow
def test_CONTROL_the_exemption_still_describes_something_real(tmp_path):
    """An exemption for a package that HAS gained a README silently stops
    exempting and starts hiding nothing -- so assert the one we carry is still
    needed, and delete it here when the auth PR lands."""
    wheels = _wheels(tmp_path / "dist")

    stale = sorted(n for n in NO_README_YET if n in wheels and _description(wheels[n]))
    assert not stale, (
        f"{stale} now has a description; remove it from NO_README_YET rather "
        "than leaving an exemption that exempts nothing"
    )
    # And with the set empty, assert that is because nothing needs exempting --
    # not because someone emptied it to make this pass.
    if not NO_README_YET:
        blank = sorted(n for n, z in wheels.items() if not _description(z))
        assert not blank, (
            f"{blank} ship no description. If you have just added one of those, "
            f'that is the whole finding: declare readme = "README.md" in its '
            f"pyproject and write the file -- hatchling sweeping a README into "
            f"the sdist is not the same as declaring it. "
            f"({_declared_at('NO_README_YET')} is the exemption list for packages "
            f"allowed to ship without one; it is empty on purpose and you almost "
            f"certainly should not add to it.)"
        )


@pytest.mark.slow
def test_CONTROL_the_reader_can_tell_a_description_from_none(tmp_path):
    """Verify description reader distinguishes populated descriptions from empty ones."""

    def wheel(description: str) -> zipfile.ZipFile:
        path = tmp_path / f"probe-{len(description)}.whl"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(
                "probe-1.0.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: probe\nVersion: 1.0.0\n\n" + description,
            )
        return zipfile.ZipFile(path)

    assert _description(wheel("a real description")) == "a real description"
    assert _description(wheel("")) == ""

    # And on the real thing, so the synthetic shape is not the only one it reads.
    real = _wheels(tmp_path / "dist")
    assert len(_description(real["cliffracer"])) > 1000, "core's description is missing"


def _sdist_has_a_licence(names: list[str]) -> bool:
    return any(n.endswith("/LICENSE") for n in names)


@pytest.mark.slow
def test_every_sdist_carries_a_licence(tmp_path):
    """The sdist is the artefact a symlinked LICENSE breaks, so check it too."""
    missing = []
    for f in _build(tmp_path / "dist"):
        if not f.name.endswith(".tar.gz"):
            continue
        with tarfile.open(f) as tf:
            if not _sdist_has_a_licence(tf.getnames()):
                missing.append(f.name.split("-")[0])
    assert not missing, f"{sorted(missing)} ship no LICENSE in their sdist"


@pytest.mark.slow
def test_CONTROL_removing_a_members_LICENSE_reds_both_checks(tmp_path):
    """Verify missing LICENSE file causes metadata verification to fail."""
    work = tmp_path / "repo"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(work), "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        # Directories with a pyproject, not everything under packages/ --
        # a .gitkeep is in there and the first version counted it, which the
        # exact-count assertion caught rather than skipping silently.
        members = sorted(
            d for d in (work / "packages").iterdir() if (d / "pyproject.toml").is_file()
        )
        # Named rather than counted in the message: `assert 11 == (11 - 1)` is
        # what this said before, which tells a reader nothing about what to do.
        expected_members = len(EXPECTED_DISTRIBUTIONS) - 1  # every distribution but core
        assert len(members) == expected_members, (
            f"packages/ holds {len(members)} member(s) with a pyproject "
            f"({sorted(m.name for m in members)}) but "
            f"{_declared_at('EXPECTED_DISTRIBUTIONS')} implies {expected_members}. "
            f"THIS CONTROL READS THE COMMITTED TREE -- `members` comes from a "
            f"`git worktree add --detach HEAD`, while the list above is read from "
            f"your working tree. So if you have just added a member and updated "
            f"that list, commit them and run this again: every other guard builds "
            f"the working tree and is already satisfied, and this one alone is "
            f"still looking at HEAD. If they ARE committed, a member was added or "
            f"removed without updating the list."
        )

        for member in members:
            victim = member / "LICENSE"
            assert victim.exists(), (
                f"{member.name} has no LICENSE in HEAD. This control reads the "
                "COMMITTED tree, so the copies must be committed before it can "
                "remove one."
            )
            victim.unlink()

            out = tmp_path / f"dist_nolicence_{member.name}"
            uv_build(["--package", member.name, "--out-dir", str(out)], work)
            wheel = next(f for f in out.iterdir() if f.name.endswith(".whl"))
            assert not _licence_entries(zipfile.ZipFile(wheel)), (
                f"removing {member.name}'s LICENSE left one in its wheel anyway, "
                "so the licence checks above cannot fail for it and prove nothing"
            )
            # The sdist half of "both", read with the sdist check's own predicate: a LICENSE
            # swept in from elsewhere (the workspace root's) would hide the member's missing one.
            sdist = next(f for f in out.iterdir() if f.name.endswith(".tar.gz"))
            with tarfile.open(sdist) as tf:
                assert not _sdist_has_a_licence(tf.getnames()), (
                    f"removing {member.name}'s LICENSE left one in its sdist anyway, "
                    "so the sdist check above cannot fail for it and prove nothing"
                )
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(work)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
