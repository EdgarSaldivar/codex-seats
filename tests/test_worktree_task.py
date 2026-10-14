from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codex_seats import router, worktree_layout
from codex_seats.workers import RunResult
from codex_seats.worktree import create, open_owned
from codex_seats.worktree_task import run_isolated_task


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


def _result() -> RunResult:
    return RunResult(0, "completed", "codex", "seat-test", "layout-test")


def test_one_shot_uses_pool_layout_and_preserves_diff_after_removal(tmp_path, monkeypatch):
    pool = tmp_path / "pool"
    pool.mkdir(mode=0o700)
    repo = _repo(tmp_path / "repo")
    observed: dict[str, Path] = {}

    head = _git(repo, "rev-parse", "HEAD").decode().strip()

    def dispatch(_root, _settings, request):
        checkout = Path(request["cd"])
        observed["checkout"] = checkout
        assert request["write_scope"] == ["."]
        assert request["_source_repository"] == str(repo)
        assert request["_source_revision"] == head
        (checkout / "tracked.txt").write_text("managed edit\n")
        (checkout / "new.txt").write_text("new\n")
        return _result()

    monkeypatch.setattr(router, "dispatch_run", dispatch)
    request = {
        "cd": str(repo),
        "_source_repository": str(tmp_path / "spoofed"),
        "_source_revision": "f" * 40,
    }
    artifact = run_isolated_task(pool, {}, request, worktree_name="one-shot-layout")
    assert request["_source_repository"] == str(tmp_path / "spoofed")
    assert request["_source_revision"] == "f" * 40

    checkout = observed["checkout"]
    assert checkout.parent.parent == pool.resolve() / worktree_layout.JOB_WORKTREES_NAME
    assert not checkout.exists()
    assert artifact.worktree_path is None
    assert artifact.changed_files == ["new.txt", "tracked.txt"]
    assert "+managed edit" in artifact.diff_text
    assert (repo / "tracked.txt").read_text() == "base\n"


def test_retained_one_shot_can_be_reopened_and_removes_managed_parent(tmp_path, monkeypatch):
    pool = tmp_path / "pool"
    pool.mkdir(mode=0o700)
    repo = _repo(tmp_path / "repo")

    def dispatch(_root, _settings, request):
        (Path(request["cd"]) / "tracked.txt").write_text("retained\n")
        return _result()

    monkeypatch.setattr(router, "dispatch_run", dispatch)
    artifact = run_isolated_task(
        pool,
        {},
        {"cd": str(repo)},
        retain=True,
        worktree_name="retained-layout",
    )
    assert artifact.worktree_path is not None
    job_parent = artifact.worktree_path.parent
    assert artifact.worktree_path.exists()

    open_owned(repo, artifact.worktree_path, artifact.branch or "").remove()

    assert not artifact.worktree_path.exists()
    assert not job_parent.exists()


def test_failed_one_shot_removes_only_its_managed_checkout(tmp_path, monkeypatch):
    pool = tmp_path / "pool"
    pool.mkdir(mode=0o700)
    repo = _repo(tmp_path / "repo")
    unrelated = pool / "unrelated.txt"
    unrelated.write_text("keep\n")
    observed: dict[str, Path] = {}

    def dispatch(_root, _settings, request):
        observed["checkout"] = Path(request["cd"])
        raise RuntimeError("injected provider failure")

    monkeypatch.setattr(router, "dispatch_run", dispatch)
    with pytest.raises(RuntimeError, match="injected provider failure"):
        run_isolated_task(
            pool, {}, {"cd": str(repo)}, worktree_name="failed-layout"
        )

    assert not observed["checkout"].exists()
    assert list((pool / worktree_layout.JOB_WORKTREES_NAME).iterdir()) == []
    assert unrelated.read_text() == "keep\n"
    assert b"failed-layout" not in _git(repo, "branch", "--list")


def test_reopening_explicit_path_does_not_claim_parent_from_its_name(tmp_path):
    repo = _repo(tmp_path / "repo")
    parent = tmp_path / "job-worktrees" / "job-user-owned"
    parent.mkdir(parents=True, mode=0o700)
    tree = create(repo, "explicit-layout-name", path=parent / "checkout")
    open_owned(repo, tree.path, tree.branch).remove()
    assert parent.is_dir()
    assert not tree.path.exists()
