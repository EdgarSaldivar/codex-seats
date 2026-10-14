import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_seats import worktree as worktree_mod
from codex_seats.worktree import MARKER_NAME, Worktree, WorktreeError, create


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    git_dir = repo / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "tags").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"
    )


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        _run(self.repo, "config", "user.email", "test@example.com")
        _run(self.repo, "config", "user.name", "Test")
        (self.repo / "hello.txt").write_text("original\n")
        _run(self.repo, "add", "hello.txt")
        _run(self.repo, "commit", "-q", "-m", "initial")
        self.made: list[Worktree] = []

    def tearDown(self):
        for wt in self.made:
            wt.remove()
        self._tmp.cleanup()

    def _make(self, branch: str) -> Worktree:
        wt = create(self.repo, branch)
        self.made.append(wt)
        return wt

    @staticmethod
    def _marker(wt: Worktree) -> Path:
        assert wt._ownership is not None
        return Path(wt._ownership["admin_dir"]) / MARKER_NAME

    def test_create_makes_isolated_checkout_with_base_content(self):
        wt = self._make("run-1")
        self.assertTrue(wt.path.exists())
        self.assertNotEqual(wt.path.resolve(), self.repo.resolve())
        self.assertEqual((wt.path / "hello.txt").read_text(), "original\n")
        self.assertEqual(wt.branch, "run-1")
        attached = subprocess.run(
            ["git", "-C", str(wt.path), "symbolic-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(attached, "refs/heads/run-1")

    def test_changes_in_worktree_do_not_touch_source(self):
        wt = self._make("run-2")
        (wt.path / "hello.txt").write_text("edited in worktree\n")
        (wt.path / "new.txt").write_text("added\n")
        # source repo working tree is untouched
        self.assertEqual((self.repo / "hello.txt").read_text(), "original\n")
        self.assertFalse((self.repo / "new.txt").exists())

    def test_diff_and_changed_files_reflect_edits_and_untracked(self):
        wt = self._make("run-3")
        (wt.path / "hello.txt").write_text("changed\n")
        (wt.path / "brand_new.txt").write_text("hi\n")
        diff = wt.diff()
        self.assertIn("hello.txt", diff)
        self.assertIn("+changed", diff)
        self.assertEqual(wt.changed_files(), ["brand_new.txt", "hello.txt"])

    def test_binary_diff_retains_delete_rename_mode_and_untracked(self):
        (self.repo / "delete.txt").write_text("delete me\n")
        (self.repo / "rename.txt").write_text("rename me\n")
        (self.repo / "mode.sh").write_text("#!/bin/sh\n")
        _run(self.repo, "add", "delete.txt", "rename.txt", "mode.sh")
        _run(self.repo, "commit", "-q", "-m", "change kinds")
        base = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # Capture directly from this disposable repository; the behavior under test is
        # the temporary-index diff, independent of worktree creation lifecycle.
        wt = Worktree(self.repo, self.repo, "capture-only", base)
        (wt.path / "delete.txt").unlink()
        (wt.path / "rename.txt").rename(wt.path / "renamed.txt")
        (wt.path / "mode.sh").chmod(0o755)
        (wt.path / "untracked.txt").write_text("untracked\n")
        (wt.path / "asset.bin").write_bytes(bytes(range(256)) + b"\x00binary")

        diff = wt.diff()

        self.assertIn("GIT binary patch", diff)
        self.assertIn("deleted file mode", diff)
        self.assertIn("rename from rename.txt", diff)
        self.assertIn("rename to renamed.txt", diff)
        self.assertIn("old mode 100644", diff)
        self.assertIn("new mode 100755", diff)
        self.assertEqual(
            wt.changed_files(),
            ["asset.bin", "delete.txt", "mode.sh", "renamed.txt", "untracked.txt"],
        )

    def test_remove_discards_checkout_and_branch(self):
        wt = create(self.repo, "run-4")
        path = wt.path
        self.assertIn("run-4", _branches(self.repo))
        wt.remove()
        self.assertFalse(path.exists())
        self.assertNotIn("run-4", _branches(self.repo))
        self.assertNotIn(str(path), _worktree_list(self.repo))

    def test_marker_is_canonical_owner_only_and_bound_to_git_admin(self):
        wt = self._make("run-owner")
        marker = self._marker(wt)
        value = json.loads(marker.read_bytes())
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(marker.stat().st_nlink, 1)
        self.assertEqual(marker.read_bytes(), worktree_mod._canonical_marker(value))
        self.assertEqual(value, wt._ownership)
        self.assertEqual(value["branch"], "refs/heads/run-owner")

    def test_missing_marker_refuses_without_deleting(self):
        wt = self._make("run-marker-missing")
        marker = self._marker(wt)
        backup = marker.with_name("marker.backup")
        marker.rename(backup)
        try:
            with self.assertRaises(WorktreeError):
                wt.remove()
            self.assertTrue(wt.path.exists())
            self.assertIn(wt.branch, _branches(self.repo))
        finally:
            backup.rename(marker)

    def test_symlink_marker_refuses_without_deleting(self):
        wt = self._make("run-marker-symlink")
        marker = self._marker(wt)
        backup = marker.with_name("marker.backup")
        marker.rename(backup)
        marker.symlink_to(backup.name)
        try:
            with self.assertRaisesRegex(WorktreeError, "regular file"):
                wt.remove()
            self.assertTrue(wt.path.exists())
        finally:
            marker.unlink()
            backup.rename(marker)

    def test_wrong_mode_and_hardlink_markers_refuse_without_deleting(self):
        wt = self._make("run-marker-metadata")
        marker = self._marker(wt)
        os.chmod(marker, 0o644)
        try:
            with self.assertRaisesRegex(WorktreeError, "owner-only"):
                wt.remove()
        finally:
            os.chmod(marker, 0o600)
        extra = marker.with_name("marker-hardlink")
        os.link(marker, extra)
        try:
            with self.assertRaisesRegex(WorktreeError, "single-link"):
                wt.remove()
            self.assertTrue(wt.path.exists())
        finally:
            extra.unlink()

    def test_marker_content_and_admin_tampering_refuse_without_deleting(self):
        wt = self._make("run-marker-content")
        marker = self._marker(wt)
        original = marker.read_bytes()
        changed = dict(wt._ownership or {})
        changed["admin_dir"] = str(marker.parent / "different")
        marker.write_bytes(worktree_mod._canonical_marker(changed))
        try:
            with self.assertRaisesRegex(WorktreeError, "contents changed"):
                wt.remove()
            self.assertTrue(wt.path.exists())
        finally:
            marker.write_bytes(original)
            os.chmod(marker, 0o600)

    def test_reconstructed_and_mismatched_objects_refuse_without_deleting(self):
        wt = self._make("run-reconstructed")
        with self.assertRaisesRegex(WorktreeError, "reconstructed"):
            Worktree(wt.repo, wt.path, wt.branch, wt.base).remove()
        forged = Worktree(
            wt.repo,
            wt.path,
            "different-branch",
            wt.base,
            _ownership=wt._ownership,
        )
        with self.assertRaisesRegex(WorktreeError, "does not match"):
            forged.remove()
        wrong_path = Worktree(
            wt.repo,
            wt.repo,
            wt.branch,
            wt.base,
            _ownership=wt._ownership,
        )
        with self.assertRaisesRegex(WorktreeError, "does not match"):
            wrong_path.remove()
        self.assertTrue(wt.path.exists())

    def test_registration_path_tampering_refuses_without_deleting(self):
        wt = self._make("run-registration")
        admin = self._marker(wt).parent
        gitdir = admin / "gitdir"
        original = gitdir.read_bytes()
        gitdir.write_text(str(wt.path.parent / "unregistered" / ".git") + "\n")
        try:
            with self.assertRaisesRegex(WorktreeError, "not registered"):
                wt.remove()
            self.assertTrue(wt.path.exists())
        finally:
            gitdir.write_bytes(original)

    def test_cleanup_git_failure_is_surfaced_without_fallback_deletion(self):
        wt = self._make("run-remove-failure")
        real_git = worktree_mod._git

        def fail_remove(repo, *args, **kwargs):
            if args[:3] == ("worktree", "remove", "--force"):
                raise WorktreeError("injected remove failure")
            return real_git(repo, *args, **kwargs)

        with mock.patch.object(worktree_mod, "_git", side_effect=fail_remove):
            with self.assertRaisesRegex(WorktreeError, "injected remove failure"):
                wt.remove()
        self.assertTrue(wt.path.exists())
        self.assertIn(wt.branch, _branches(self.repo))

    def test_restore_paths_purges_untracked_under_a_protected_dir(self):
        # A protected DIRECTORY must come back pristine: git diff/checkout miss
        # untracked additions, so a head could smuggle a conftest.py into a
        # protected test dir and skew the judge. restore_paths must remove it.
        _run(self.repo, "config", "core.excludesfile", "/dev/null")
        (self.repo / "guard").mkdir()
        (self.repo / "guard" / "keep.txt").write_text("keep\n")
        _run(self.repo, "add", "guard/keep.txt")
        _run(self.repo, "commit", "-q", "-m", "add guard")
        wt = self._make("run-guard")
        (wt.path / "guard" / "sneaky.py").write_text("import os\n")  # untracked
        (wt.path / "guard" / "keep.txt").write_text("tampered\n")     # tracked edit
        tampered = wt.restore_paths(["guard"])
        self.assertIn("guard", tampered)
        self.assertFalse((wt.path / "guard" / "sneaky.py").exists())  # untracked purged
        self.assertEqual((wt.path / "guard" / "keep.txt").read_text(), "keep\n")

    def test_remove_is_idempotent(self):
        wt = create(self.repo, "run-5")
        wt.remove()
        wt.remove()  # must not raise

    def test_context_manager_auto_removes(self):
        with create(self.repo, "run-6") as wt:
            path = wt.path
            self.assertTrue(path.exists())
        self.assertFalse(path.exists())
        self.assertNotIn("run-6", _branches(self.repo))

    def test_create_on_non_git_dir_raises(self):
        with tempfile.TemporaryDirectory() as plain:
            with self.assertRaises(WorktreeError):
                create(plain, "run-x")

    def test_invalid_branch_and_base_fail_without_artifacts(self):
        before_branches = _branches(self.repo)
        before_worktrees = _worktree_list(self.repo)
        with self.assertRaises(WorktreeError):
            create(self.repo, "../unsafe")
        with self.assertRaises(WorktreeError):
            create(self.repo, "valid-unused", base="--not-a-revision")
        self.assertEqual(_branches(self.repo), before_branches)
        self.assertEqual(_worktree_list(self.repo), before_worktrees)

    def test_post_checkout_hook_is_not_executed(self):
        hook_output = self.repo.parent / "hook-ran"
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\ntouch {hook_output}\n")
        hook.chmod(0o700)
        self._make("run-no-hooks")
        self.assertFalse(hook_output.exists())

    def test_marker_creation_failure_rolls_back_owned_branch_and_worktree(self):
        before_branches = _branches(self.repo)
        before_worktrees = _worktree_list(self.repo)
        with mock.patch.object(
            worktree_mod, "_write_marker", side_effect=OSError("injected marker failure")
        ):
            with self.assertRaisesRegex(WorktreeError, "marker failure"):
                create(self.repo, "run-marker-rollback")
        self.assertEqual(_branches(self.repo), before_branches)
        self.assertEqual(_worktree_list(self.repo), before_worktrees)

    def test_explicit_parent_must_be_owner_controlled(self):
        parent = Path(self._tmp.name) / "shared"
        parent.mkdir(mode=0o700)
        os.chmod(parent, 0o777)
        target = parent / "checkout"
        with self.assertRaisesRegex(WorktreeError, "owner-controlled"):
            create(self.repo, "run-unsafe-parent", path=target)
        self.assertFalse(target.exists())
        self.assertNotIn("run-unsafe-parent", _branches(self.repo))

    def test_link_symlinks_gitignored_paths_into_worktree(self):
        # a gitignored tool dir in the source repo, not carried by the checkout
        (self.repo / "toollib").mkdir()
        (self.repo / "toollib" / "run.sh").write_text("echo hi\n")
        wt = self._make("run-link")
        # not present until linked
        self.assertFalse((wt.path / "toollib").exists())
        wt.remove()
        wt = create(self.repo, "run-link2", link=["toollib"])
        self.made.append(wt)
        linked = wt.path / "toollib"
        self.assertTrue(linked.is_symlink())
        self.assertEqual((linked / "run.sh").read_text(), "echo hi\n")

    def test_restore_paths_reverts_edits_and_reports_tampering(self):
        wt = self._make("run-restore")
        (wt.path / "hello.txt").write_text("weakened by head\n")
        tampered = wt.restore_paths(["hello.txt"])
        self.assertEqual(tampered, ["hello.txt"])
        self.assertEqual((wt.path / "hello.txt").read_text(), "original\n")
        # untouched afterwards -> no tampering reported
        self.assertEqual(wt.restore_paths(["hello.txt"]), [])

    def test_restore_paths_restores_deleted_and_removes_created(self):
        wt = self._make("run-restore2")
        (wt.path / "hello.txt").unlink()          # head deleted a protected file
        (wt.path / "sneaky.txt").write_text("x")  # head created a protected path not at base
        tampered = wt.restore_paths(["hello.txt", "sneaky.txt"])
        self.assertEqual(sorted(tampered), ["hello.txt", "sneaky.txt"])
        self.assertEqual((wt.path / "hello.txt").read_text(), "original\n")
        self.assertFalse((wt.path / "sneaky.txt").exists())

    def test_create_rejects_existing_path(self):
        target = Path(self._tmp.name).resolve() / "taken"
        target.mkdir()
        with self.assertRaises(WorktreeError) as ctx:
            create(self.repo, "run-7", path=target)
        self.assertIn("already exists", str(ctx.exception))


def _branches(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "branch", "--list"],
        capture_output=True, text=True,
    ).stdout


def _worktree_list(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True, text=True,
    ).stdout


if __name__ == "__main__":
    unittest.main()
