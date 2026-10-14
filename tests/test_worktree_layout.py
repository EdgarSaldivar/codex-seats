from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codex_seats import worktree_layout
from codex_seats.worktree import WorktreeError, create, create_managed


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    ).stdout


def _repo(path: Path) -> Path:
    path.mkdir(mode=0o700)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("base\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "initial")
    return path.resolve()


def test_managed_checkout_uses_owner_only_pool_layout_and_removes_job_parent(tmp_path):
    pool = tmp_path / "pool"
    pool.mkdir(mode=0o700)
    repo = _repo(tmp_path / "repo")

    tree = create_managed(pool, repo, "managed-layout")
    checkout = tree.path
    job_parent = checkout.parent
    assert checkout == job_parent / "checkout"
    assert job_parent.parent == pool.resolve() / worktree_layout.JOB_WORKTREES_NAME
    assert job_parent.stat().st_mode & 0o777 == 0o700
    assert job_parent.parent.stat().st_mode & 0o777 == 0o700
    assert tree._ownership is not None
    assert tree._ownership["worktree_path"] == str(checkout)

    tree.remove()

    assert not checkout.exists()
    assert not job_parent.exists()
    assert job_parent.parent.is_dir()


def test_managed_creation_failure_rolls_back_allocation_without_touching_legacy(tmp_path):
    pool = tmp_path / "pool"
    pool.mkdir(mode=0o700)
    repo = _repo(tmp_path / "repo")
    legacy_parent = tmp_path / "legacy"
    legacy_parent.mkdir(mode=0o700)
    legacy = create(repo, "existing-branch", path=legacy_parent / "checkout")
    try:
        with pytest.raises(WorktreeError, match="already exists"):
            create_managed(pool, repo, "existing-branch")
        managed_parent = pool / worktree_layout.JOB_WORKTREES_NAME
        assert list(managed_parent.iterdir()) == []
        assert legacy.path.exists()
        assert b"existing-branch" in _git(repo, "branch", "--list")
    finally:
        legacy.remove()


def test_managed_creation_rejects_symlinked_layout_parent(tmp_path):
    pool = tmp_path / "pool"
    pool.mkdir(mode=0o700)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(mode=0o700)
    (pool / worktree_layout.JOB_WORKTREES_NAME).symlink_to(
        unrelated, target_is_directory=True
    )
    repo = _repo(tmp_path / "repo")

    with pytest.raises(WorktreeError, match="canonical owner-controlled"):
        create_managed(pool, repo, "symlinked-parent")

    assert list(unrelated.iterdir()) == []
    assert b"symlinked-parent" not in _git(repo, "branch", "--list")
