"""Judge for the link-exclusion hardening goal.

Regression (2026-07-19, production goal runs g1784445822-6/-8): the repo
preset linked ``backend/.venv`` into each worktree, but the repo's gitignore
pattern carried a trailing slash (``backend/.venv/``), which matches
DIRECTORIES only — not the symlink ``create(link=...)`` places. The symlink was
therefore staged by ``_full_change``'s ``git add -A`` and surfaced in every
review diff, and the review gate (correctly) blocked each one. ~8.8M seat
tokens burned before diagnosis.

Contract pinned here: paths named in ``link=`` are declared INFRASTRUCTURE.
They must never appear in ``diff()`` / ``changed_files()`` output, regardless
of how the repo's gitignore is spelled — while real work in the worktree,
including untracked files, still must.
"""

import tempfile
import unittest
from pathlib import Path

import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from codex_seats.worktree import Worktree, create  # noqa: E402


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class LinkExclusionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _run(self.repo, "init", "-q")
        _run(self.repo, "config", "user.email", "test@example.com")
        _run(self.repo, "config", "user.name", "Test")
        (self.repo / "hello.txt").write_text("original\n")
        # the regression shape: gitignore names the tool dir with a TRAILING
        # SLASH, so the pattern matches only real directories — not the symlink
        # the worktree infra creates.
        (self.repo / ".gitignore").write_text("tooldir/\n")
        _run(self.repo, "add", "hello.txt", ".gitignore")
        _run(self.repo, "commit", "-q", "-m", "initial")
        (self.repo / "tooldir").mkdir()
        (self.repo / "tooldir" / "bin").write_text("#!/bin/sh\n")
        self.made: list[Worktree] = []

    def tearDown(self):
        for wt in self.made:
            wt.remove()
        self._tmp.cleanup()

    def _make(self, branch: str, **kwargs) -> Worktree:
        wt = create(self.repo, branch, **kwargs)
        self.made.append(wt)
        return wt

    def test_linked_symlink_never_reaches_diff_despite_dir_only_gitignore(self):
        wt = self._make("regress", link=["tooldir"])
        self.assertTrue((wt.path / "tooldir").is_symlink(), "precondition: link placed")
        self.assertNotIn("tooldir", wt.changed_files())
        self.assertNotIn("tooldir", wt.diff())

    def test_linked_path_excluded_even_when_not_gitignored_at_all(self):
        # structural exclusion: link= is the declaration; gitignore spelling is
        # irrelevant. "otherdir" is not ignored in any form.
        (self.repo / "otherdir").mkdir()
        (self.repo / "otherdir" / "data").write_text("x\n")
        wt = self._make("structural", link=["otherdir"])
        self.assertTrue((wt.path / "otherdir").is_symlink(), "precondition: link placed")
        self.assertNotIn("otherdir", wt.changed_files())
        self.assertNotIn("otherdir", wt.diff())

    def test_linked_gitignored_symlink_does_not_error(self):
        # Regression (2026-07-22): when the gitignore pattern has NO
        # trailing slash (`.venv`), it matches the symlink `create(link=...)` places,
        # and the old `git add -A -- . :(exclude,literal).venv` FAILED with
        # "The following paths are ignored ... Use -f" — so `_full_change` raised and
        # NO backend/iOS goal (whose acceptance needs the linked venv/models) could be
        # supervised. The fix stages then unstages links, so this must not raise and
        # the link must stay out of the diff.
        (self.repo / ".gitignore").write_text("tooldir/\nsecretenv\n")  # `secretenv` = no slash
        _run(self.repo, "add", ".gitignore")
        _run(self.repo, "commit", "-q", "-m", "ignore secretenv")
        (self.repo / "secretenv").mkdir()
        (self.repo / "secretenv" / "bin").write_text("py\n")
        wt = self._make("gitignored-link", link=["secretenv"])
        self.assertTrue((wt.path / "secretenv").is_symlink(), "precondition: link placed")
        # These must not raise (the bug made them raise WorktreeError):
        changed = wt.changed_files()
        diff = wt.diff()
        self.assertNotIn("secretenv", changed)
        self.assertNotIn("secretenv", diff)

    def test_real_work_still_appears_alongside_excluded_links(self):
        wt = self._make("work", link=["tooldir"])
        (wt.path / "hello.txt").write_text("edited\n")
        (wt.path / "brand_new.py").write_text("print('hi')\n")
        changed = wt.changed_files()
        self.assertIn("hello.txt", changed)
        self.assertIn("brand_new.py", changed)
        self.assertNotIn("tooldir", changed)
        diff = wt.diff()
        self.assertIn("brand_new.py", diff)
        self.assertNotIn("tooldir", diff)


if __name__ == "__main__":
    unittest.main()
