"""Tests ensuring built wheels carry valid descriptions and licenses."""

import email
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _require_git():
    if not (ROOT / ".git").is_dir():
        pytest.skip("Not running inside a git repository (release tarball)")


# Exact distribution set expected in builds.
EXPECTED_DISTRIBUTIONS = {
    "cliffracer",
    "cliffracer_auth",
    "cliffracer_backdoor",
    "cliffracer_cron",
    "cliffracer_faststream",
    "cliffracer_http",
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


def _build(out: Path) -> list[Path]:
    proc = subprocess.run(
        ["uv", "build", "--all-packages", "--out-dir", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"uv build failed:\n{proc.stdout}\n{proc.stderr}"
    # uv writes a .gitignore into --out-dir; artefacts are picked by suffix.
    return sorted(f for f in out.iterdir() if f.name.endswith((".whl", ".tar.gz")))


def _wheels(out: Path) -> dict[str, zipfile.ZipFile]:
    return {f.name.split("-")[0]: zipfile.ZipFile(f) for f in _build(out) if f.suffix == ".whl"}


def _description(z: zipfile.ZipFile) -> str:
    name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
    return (email.message_from_bytes(z.read(name)).get_payload() or "").strip()


def _licence_entries(z: zipfile.ZipFile) -> list[str]:
    return [n for n in z.namelist() if "/licenses/" in n or n.endswith(".dist-info/LICENSE")]


@pytest.mark.slow
def test_every_wheel_carries_a_licence(tmp_path):
    wheels = _wheels(tmp_path / "dist")
    assert set(wheels) == EXPECTED_DISTRIBUTIONS, (
        f"built {sorted(wheels)}, expected {sorted(EXPECTED_DISTRIBUTIONS)}"
    )

    missing = sorted(n for n, z in wheels.items() if not _licence_entries(z))
    assert not missing, (
        f"{missing} declare MIT in their metadata and ship no LICENSE file. "
        'Add license-files = ["LICENSE"] and copy the file into the package.'
    )


@pytest.mark.slow
def test_every_wheel_carries_a_description(tmp_path):
    wheels = _wheels(tmp_path / "dist")

    blank = sorted(n for n, z in wheels.items() if not _description(z) and n not in NO_README_YET)
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
            f"NO_README_YET is empty but {blank} still have no description; "
            "the exemption was removed without the README landing"
        )


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


@pytest.mark.slow
def test_every_sdist_carries_a_licence(tmp_path):
    """The sdist is the artefact a symlinked LICENSE breaks, so check it too."""
    missing = []
    for f in _build(tmp_path / "dist"):
        if not f.name.endswith(".tar.gz"):
            continue
        with tarfile.open(f) as tf:
            if not any(n.endswith("/LICENSE") for n in tf.getnames()):
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
        assert len(members) == len(EXPECTED_DISTRIBUTIONS) - 1, sorted(m.name for m in members)

        for member in members:
            victim = member / "LICENSE"
            assert victim.exists(), (
                f"{member.name} has no LICENSE in HEAD. This control reads the "
                "COMMITTED tree, so the copies must be committed before it can "
                "remove one."
            )
            victim.unlink()

            out = tmp_path / f"dist_nolicence_{member.name}"
            proc = subprocess.run(
                ["uv", "build", "--package", member.name, "--out-dir", str(out)],
                cwd=work,
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, proc.stderr
            wheel = next(f for f in out.iterdir() if f.name.endswith(".whl"))
            assert not _licence_entries(zipfile.ZipFile(wheel)), (
                f"removing {member.name}'s LICENSE left one in its wheel anyway, "
                "so the licence checks above cannot fail for it and prove nothing"
            )
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(work)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
