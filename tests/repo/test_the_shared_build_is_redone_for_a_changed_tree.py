"""The packaging guards' shared build is redone when the tree it built changes.

`build_all` reuses a build while `tree_key` is unchanged. The key changes for an edit that keeps
a file's size and timestamp, for a file added beside the tracked ones, and for a new commit, from
which the version is derived; it stays the same for a tree nothing touched.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.repo.built_distributions import tree_key

pytestmark = pytest.mark.repo


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def tree(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "member.py").write_text("VALUE = 1\n")
    os.utime(tmp_path / "member.py", (1_700_000_000, 1_700_000_000))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "first")
    return tmp_path


def test_an_edit_that_keeps_size_and_timestamp_changes_the_key(tree):
    before = tree_key(tree)
    (tree / "member.py").write_text("VALUE = 2\n")
    os.utime(tree / "member.py", (1_700_000_000, 1_700_000_000))

    assert tree_key(tree) != before


def test_an_added_file_changes_the_key(tree):
    before = tree_key(tree)
    (tree / "added.py").write_text("")

    assert tree_key(tree) != before


def test_a_new_commit_changes_the_key(tree):
    before = tree_key(tree)
    _git(tree, "commit", "-q", "--allow-empty", "-m", "second")

    assert tree_key(tree) != before


def test_CONTROL_an_untouched_tree_keeps_its_key(tree):
    assert tree_key(tree) == tree_key(tree)
