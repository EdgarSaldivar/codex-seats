from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import pytest
from codex_seats import changesets
from codex_seats import dispatch_protocol as dp

RELEASE = "d" * 64


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    env = dict(os.environ)
    env["GIT_CEILING_DIRECTORIES"] = str(repo)
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout


def _repo(path: Path) -> tuple[Path, str]:
    path.mkdir()
    # Build the minimal empty repository layout directly. This keeps the integration
    # tests runnable in managed-worker sandboxes that forbid Git's pre-init discovery
    # walk above the declared worktree.
    git_dir = path / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "tags").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"
    )
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("before\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "base")
    return path, _git(path, "rev-parse", "HEAD").decode().strip()


def _capture(repo: Path, edits: dict[str, bytes | None]) -> tuple[bytes, list[str]]:
    for name, content in edits.items():
        target = repo / name
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    _git(repo, "add", "-A")
    patch = _git(
        repo,
        "diff",
        "--cached",
        "--binary",
        "--full-index",
        "--find-renames",
        "HEAD",
    )
    names = sorted(
        item.decode("utf-8", "surrogateescape")
        for item in _git(repo, "diff", "--cached", "--name-only", "-z", "HEAD").split(b"\0")
        if item
    )
    _git(repo, "reset", "--hard", "-q", "HEAD")
    return patch, names


def _create(
    root: Path,
    base: str,
    patch: bytes,
    files: list[str],
    *,
    repo_id: str = "sample",
    request_id: str | None = None,
) -> changesets.VerifiedChangeSet:
    return changesets.create(
        root,
        request_id=request_id or dp.new_request_id(),
        request_hash="sha256:" + "a" * 64,
        release_id=RELEASE,
        protocol_version=dp.PROTOCOL_VERSION,
        repo_id=repo_id,
        base_commit=base,
        changed_files=files,
        terminal_status="completed",
        patch=patch,
        now=1234,
    )


def _settings(repo: Path, repo_id: str = "sample") -> dict:
    return {"projects": {repo_id: {"path": str(repo)}}}


def test_create_is_atomic_owner_only_and_verifies_every_hash(tmp_path):
    base = "b" * 40
    patch = b"diff --git a/tracked.txt b/tracked.txt\n"
    names = ["new.txt", "tracked.txt"]
    item = _create(tmp_path / "control", base, patch, names)

    assert item.manifest["patch_sha256"].startswith("sha256:")
    assert item.manifest["changed_files"] == ["new.txt", "tracked.txt"]
    assert item.directory.stat().st_mode & 0o777 == 0o700
    assert (item.directory / changesets.PATCH_NAME).stat().st_mode & 0o777 == 0o400
    assert changesets.inspect(tmp_path / "control", item.id).manifest_sha256 == item.manifest_sha256

    patch_path = item.directory / changesets.PATCH_NAME
    os.chmod(patch_path, 0o600)
    patch_path.write_bytes(patch + b"tampered")
    os.chmod(patch_path, 0o400)
    with pytest.raises(changesets.ChangeSetError, match="SHA-256|patch_size"):
        changesets.inspect(tmp_path / "control", item.id)


def test_manifest_tampering_is_detected_against_terminal_receipt(tmp_path):
    root = tmp_path / "control"
    item = _create(root, "b" * 40, b"patch\n", ["tracked.txt"])
    dp.write_receipt(
        root,
        {
            "request_id": item.id,
            "changeset_id": item.id,
            "changeset_sha": item.manifest_sha256,
        },
    )
    manifest_path = item.directory / changesets.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["created_at"] += 1
    os.chmod(manifest_path, 0o600)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    os.chmod(manifest_path, 0o400)

    with pytest.raises(changesets.ChangeSetError, match="terminal receipt"):
        changesets.inspect(root, item.id)


def test_inspect_rejects_symlink_and_list_is_read_only(tmp_path):
    missing = tmp_path / "absent"
    assert changesets.list_all(missing) == []
    assert not missing.exists()

    storage = changesets.ensure_storage(tmp_path / "control")
    change_id = dp.new_request_id()
    (storage["active"] / change_id).symlink_to(tmp_path)
    with pytest.raises(changesets.ChangeSetError, match="unsafe ChangeSet directory"):
        changesets.inspect(tmp_path / "control", change_id)


def test_clean_text_apply_stages_exact_files_and_records_event(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    repo, base = _repo(tmp_path / "repo")
    patch, names = _capture(repo, {"tracked.txt": b"after\n", "added.txt": b"hello\n"})
    root = tmp_path / "control"
    item = _create(root, base, patch, names)

    event = changesets.apply(root, _settings(repo), item.id)

    assert (repo / "tracked.txt").read_text() == "after\n"
    assert (repo / "added.txt").read_text() == "hello\n"
    assert _git(repo, "diff", "--cached", "--name-only").decode().splitlines() == names
    assert event["pre_apply_head"] == base
    event_path = changesets.paths(root)["applied"] / f"{item.id}.json"
    assert event_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(event_path.read_text())["changeset_sha256"] == item.manifest_sha256
    assert changesets.drop(root, item.id) == "dropped"
    assert (repo / "tracked.txt").read_text() == "after\n"


def test_binary_addition_applies_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    repo, base = _repo(tmp_path / "repo")
    binary = bytes(range(256)) * 4 + b"\x00\xffbinary"
    patch, names = _capture(repo, {"asset.bin": binary})
    assert b"GIT binary patch" in patch
    item = _create(tmp_path / "control", base, patch, names)

    changesets.apply(tmp_path / "control", _settings(repo), item.id)

    assert (repo / "asset.bin").read_bytes() == binary
    assert _git(repo, "diff", "--cached", "--name-only").strip() == b"asset.bin"


def test_binary_modification_applies_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    repo, _base = _repo(tmp_path / "repo")
    (repo / "asset.bin").write_bytes(b"old\x00binary")
    _git(repo, "add", "asset.bin")
    _git(repo, "commit", "-qm", "binary base")
    base = _git(repo, "rev-parse", "HEAD").decode().strip()
    replacement = bytes(range(255, -1, -1)) * 5
    patch, names = _capture(repo, {"asset.bin": replacement})
    item = _create(tmp_path / "control", base, patch, names)

    changesets.apply(tmp_path / "control", _settings(repo), item.id)

    assert (repo / "asset.bin").read_bytes() == replacement


@pytest.mark.parametrize("dirty_kind", ["staged", "unstaged", "untracked"])
def test_apply_refuses_every_kind_of_user_work(tmp_path, monkeypatch, dirty_kind):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    repo, base = _repo(tmp_path / "repo")
    patch, names = _capture(repo, {"tracked.txt": b"worker\n"})
    item = _create(tmp_path / "control", base, patch, names)
    if dirty_kind == "staged":
        (repo / "tracked.txt").write_text("user\n")
        _git(repo, "add", "tracked.txt")
    elif dirty_kind == "unstaged":
        (repo / "tracked.txt").write_text("user\n")
    else:
        (repo / "user.txt").write_text("user\n")

    with pytest.raises(changesets.ChangeSetError, match="user work"):
        changesets.apply(tmp_path / "control", _settings(repo), item.id)


def test_actual_git_apply_failure_is_clean_without_destructive_rollback(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    repo, base = _repo(tmp_path / "repo")
    patch, names = _capture(repo, {"tracked.txt": b"worker\n"})
    item = _create(tmp_path / "control", base, patch, names)
    real_git = changesets._git

    def fail_actual(repo_path, *args, **kwargs):
        if args[:3] == ("apply", "--index", "--binary"):
            return subprocess.CompletedProcess(args, 1, b"", b"simulated atomic failure")
        return real_git(repo_path, *args, **kwargs)

    monkeypatch.setattr(changesets, "_git", fail_actual)
    with pytest.raises(changesets.ChangeSetError, match="without mutation"):
        changesets.apply(tmp_path / "control", _settings(repo), item.id)

    assert _git(repo, "status", "--porcelain") == b""
    assert (repo / "tracked.txt").read_text() == "before\n"


def test_apply_resolves_current_registry_and_refuses_managed_worker(tmp_path, monkeypatch):
    item = _create(
        tmp_path / "control",
        "b" * 40,
        b"patch\n",
        ["tracked.txt"],
        repo_id="registered",
    )
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    with pytest.raises(changesets.ChangeSetError, match="managed workers"):
        changesets.apply(tmp_path / "control", {}, item.id)
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER")
    with pytest.raises(changesets.ChangeSetError, match="not currently registered"):
        changesets.apply(tmp_path / "control", {}, item.id)

    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    with pytest.raises(changesets.ChangeSetError, match="managed workers"):
        changesets.drop(tmp_path / "control", item.id)


def test_manifest_rejects_git_control_paths(tmp_path):
    with pytest.raises(changesets.ChangeSetError, match="unsafe changed file"):
        _create(
            tmp_path / "control",
            "b" * 40,
            b"patch\n",
            [".git/config"],
        )


def test_apply_rejects_symlinked_git_control_path(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    repo, base = _repo(tmp_path / "repo")
    patch, names = _capture(repo, {"tracked.txt": b"worker\n"})
    item = _create(tmp_path / "control", base, patch, names)
    actual = repo / ".git-real"
    (repo / ".git").rename(actual)
    (repo / ".git").symlink_to(actual)

    with pytest.raises(changesets.ChangeSetError, match="Git control path"):
        changesets.apply(tmp_path / "control", _settings(repo), item.id)


def test_apply_rejects_symlinked_git_index(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    repo, base = _repo(tmp_path / "repo")
    patch, names = _capture(repo, {"tracked.txt": b"worker\n"})
    item = _create(tmp_path / "control", base, patch, names)
    index = repo / ".git" / "index"
    real_index = repo / ".git" / "index.real"
    index.rename(real_index)
    index.symlink_to(real_index)

    with pytest.raises(changesets.ChangeSetError, match="index path is symlinked"):
        changesets.apply(tmp_path / "control", _settings(repo), item.id)


def test_apply_refuses_missing_and_nonancestor_base(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    repo, base = _repo(tmp_path / "repo")
    root = tmp_path / "control"
    missing = _create(root, "f" * 40, b"", [], request_id=dp.new_request_id())
    with pytest.raises(changesets.ChangeSetError, match="base commit is missing"):
        changesets.apply(root, _settings(repo), missing.id)

    (repo / "tracked.txt").write_text("descendant\n")
    _git(repo, "commit", "-qam", "descendant")
    descendant = _git(repo, "rev-parse", "HEAD").decode().strip()
    _git(repo, "checkout", "-q", "--detach", base)
    nonancestor = _create(root, descendant, b"", [], request_id=dp.new_request_id())
    with pytest.raises(changesets.ChangeSetError, match="not an ancestor"):
        changesets.apply(root, _settings(repo), nonancestor.id)


def test_preflight_and_exact_set_failures_leave_repo_clean(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    repo, base = _repo(tmp_path / "repo")
    root = tmp_path / "control"
    bad = _create(root, base, b"not a patch\n", ["tracked.txt"])
    with pytest.raises(changesets.ChangeSetError, match="does not apply cleanly"):
        changesets.apply(root, _settings(repo), bad.id)
    assert _git(repo, "status", "--porcelain") == b""
    assert (repo / "tracked.txt").read_text() == "before\n"

    patch, _names = _capture(repo, {"tracked.txt": b"after\n"})
    mismatch = _create(root, base, patch, ["different.txt"])
    objects_before = _git(repo, "count-objects", "-v")
    with pytest.raises(changesets.ChangeSetError, match="staged changed-file set"):
        changesets.apply(root, _settings(repo), mismatch.id)
    assert _git(repo, "status", "--porcelain") == b""
    assert (repo / "tracked.txt").read_text() == "before\n"
    assert _git(repo, "count-objects", "-v") == objects_before


def test_recoverable_drop_is_verified_and_idempotent(tmp_path, monkeypatch):
    # This controller action is exercised only against the disposable fixture.
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    monkeypatch.delenv("CLAUDE_LEAD_WORKER", raising=False)
    root = tmp_path / "control"
    item = _create(root, "b" * 40, b"", [])
    assert changesets.drop(root, item.id) == "dropped"
    assert changesets.inspect(root, item.id).state == "dropped"
    assert changesets.drop(root, item.id) == "already_dropped"
    assert [row.state for row in changesets.list_all(root)] == ["dropped"]


def test_cli_list_and_inspect_are_concise_and_patch_is_explicit(tmp_path, capsys):
    root = tmp_path / "control"
    item = _create(root, "b" * 40, b"secret patch bytes\n", ["tracked.txt"])
    args = argparse.Namespace(changeset_action="inspect", id=item.id, patch=False, json=False)
    assert changesets.cmd_changeset(args, {}, root) == 0
    assert "secret patch bytes" not in capsys.readouterr().out

    args.patch = True
    assert changesets.cmd_changeset(args, {}, root) == 0
    assert "secret patch bytes" in capsys.readouterr().out


def test_changeset_cli_family_registers_all_actions():
    parser = argparse.ArgumentParser()
    changesets.register(parser.add_subparsers(dest="command", required=True))

    for action in ("list", "inspect", "apply", "drop"):
        argv = ["changeset", action]
        if action != "list":
            argv.append("a" * 32)
        args = parser.parse_args(argv)
        assert args.handler == "changeset"
        assert args.changeset_action == action
