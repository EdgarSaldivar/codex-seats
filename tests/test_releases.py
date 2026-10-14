from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from codex_seats import cli
from codex_seats import releases


def _source(parent: Path, name: str = "source") -> Path:
    repo = parent / name
    repo.mkdir()
    (repo / "codex-seat").write_text("#!/bin/sh\necho release\n")
    (repo / "lib").mkdir()
    (repo / "lib" / "payload.txt").write_text("payload\n")
    os.chmod(repo / "codex-seat", 0o755)
    return repo


def _runtime_source(parent: Path, name: str = "runtime-source") -> Path:
    repo = parent / name
    package = repo / "lib" / "codex_seats"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "import os\nprint('cli:' + os.environ['CODEX_SEATS_RELEASE_ID'])\n"
    )
    (package / "worker_hook.py").write_text(
        "import os\nprint('hook:' + os.environ['CODEX_SEATS_RELEASE_ID'])\n"
    )
    bootstrap = repo / "bootstrap"
    bootstrap.mkdir()
    project = Path(__file__).resolve().parents[1]
    for name in ("codex-seat", "worker-hook"):
        shutil.copy2(project / "bootstrap" / name, bootstrap / name)
        os.chmod(bootstrap / name, 0o755)
    return repo


def _stage(
    root: Path,
    source: Path,
    revision: str = "1",
    *,
    protocol_compatibility=None,
) -> str:
    commit = revision * 40
    tree = revision * 40

    def inspect(path):
        return Path(path).resolve(), commit, tree

    def build(path, destination, _commit):
        for child in Path(path).iterdir():
            target = destination / child.name
            if child.is_symlink():
                os.symlink(os.readlink(child), target)
            elif child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)

    return releases.stage_release(
        root,
        source,
        protocol_compatibility=protocol_compatibility,
        inspect_source=inspect,
        build_source=build,
    )


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_runtime_release_selection_rejects_paths_and_symlink_escapes(self) -> None:
        for value in (
            "../" + "a" * 64,
            "sha256:" + "a" * 64,
            "A" * 64,
            "a" * 63,
        ):
            with self.assertRaises(releases.ReleaseError):
                releases.validate_release_id(value)

        root = self.temp / "root"
        (root / "releases").mkdir(parents=True)
        outside = self.temp / "outside"
        outside.mkdir()
        (root / "releases" / ("a" * 64)).symlink_to(
            outside, target_is_directory=True
        )
        (root / "current").symlink_to(f"releases/{'a' * 64}")
        with self.assertRaisesRegex(releases.ReleaseError, "release does not exist"):
            releases.current_release_id(root)

        (root / "current").unlink()
        (root / "current").symlink_to("../outside")
        with self.assertRaisesRegex(releases.ReleaseError, "unsafe target"):
            releases.current_release_id(root)

    def test_loaded_module_pin_survives_current_pointer_switch(self) -> None:
        root = self.temp / "root"
        first, second = "a" * 64, "b" * 64
        module = root / "releases" / first / "lib" / "codex_seats" / "client.py"
        module.parent.mkdir(parents=True)
        module.write_text("")
        (root / "releases" / second).mkdir()
        (root / "current").symlink_to(f"releases/{first}")
        self.assertEqual(releases.loaded_release_id(root, module), first)
        replacement = root / ".current.next"
        replacement.symlink_to(f"releases/{second}")
        replacement.replace(root / "current")
        self.assertEqual(releases.current_release_id(root), second)
        self.assertEqual(releases.loaded_release_id(root, module), first)

    def test_stage_binds_git_source_and_content_then_publishes_read_only(self) -> None:
        root = self.temp / "root"
        source = _source(self.temp)

        digest = _stage(root, source)
        directory = root / "releases" / digest
        manifest = releases.verify_release(root, digest)

        self.assertEqual(len(digest), 64)
        self.assertEqual(manifest["source"]["commit"], "1" * 40)
        self.assertEqual(manifest["source"]["tree"], "1" * 40)
        self.assertTrue(manifest["source"]["content_sha256"].startswith("sha256:"))
        self.assertEqual(
            [item["path"] for item in manifest["files"]],
            ["codex-seat", "lib/payload.txt"],
        )
        self.assertEqual(
            manifest["protocol_compatibility"], releases.DEFAULT_COMPATIBILITY
        )
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o555)
        self.assertEqual(
            stat.S_IMODE((directory / "manifest.json").stat().st_mode), 0o444
        )
        self.assertEqual(stat.S_IMODE((directory / "codex-seat").stat().st_mode), 0o555)
        self.assertFalse(list((root / "releases").glob(".stage-*")))

        # Re-staging the same immutable input is idempotent.
        self.assertEqual(_stage(root, source), digest)
        self.assertEqual(
            [path.name for path in (root / "releases").iterdir()], [digest]
        )

    def test_stage_refuses_dirty_source_without_publishing(self) -> None:
        source = _source(self.temp)
        (source / "untracked.txt").write_text("not committed\n")

        with mock.patch.object(
            releases,
            "_git",
            side_effect=[str(source), b"?? untracked.txt\0"],
        ):
            with self.assertRaisesRegex(releases.ReleaseError, "not clean"):
                releases._clean_source(source)

    def test_verify_rejects_content_tampering(self) -> None:
        root = self.temp / "root"
        source = _source(self.temp)
        digest = _stage(root, source)
        payload = root / "releases" / digest / "lib" / "payload.txt"
        os.chmod(payload, 0o644)
        payload.write_text("tampered\n")

        with self.assertRaisesRegex(releases.ReleaseError, "content verification failed"):
            releases.verify_release(root, digest)

    def test_verify_rejects_mutable_permissions_and_hashes_symlinks(self) -> None:
        root = self.temp / "root"
        source = _source(self.temp)
        os.symlink("lib/payload.txt", source / "payload-link")
        digest = _stage(root, source)
        manifest = releases.verify_release(root, digest)
        link = next(item for item in manifest["files"] if item["path"] == "payload-link")
        self.assertEqual(link["type"], "symlink")
        self.assertEqual(link["mode"], "120000")

        payload = root / "releases" / digest / "lib" / "payload.txt"
        os.chmod(payload, 0o644)
        with self.assertRaisesRegex(releases.ReleaseError, "not immutable"):
            releases.verify_release(root, digest)

    def test_bootstrap_preflight_is_read_only_and_reports_every_boundary(self) -> None:
        root = self.temp / "root"
        root.mkdir()
        ledger = {
            "seats": {"seat1": {"in_flight": {"dispatcher_pid": 123}}},
            "providers": {"claude": {"in_flight": {"child_pid": 456}}},
            "goal_queue": [{"goal_id": "goal-a", "status": "running", "pid": 111}],
            "campaigns": [{"run_id": "campaign-a", "status": "running", "pid": 222}],
        }
        ledger_path = root / "seats.json"
        ledger_path.write_text(json.dumps(ledger))
        inbox = root / "dispatch" / "inbox"
        processing = root / "dispatch" / "processing"
        inbox.mkdir(parents=True)
        processing.mkdir()
        (inbox / "one.ready").write_text("{}")
        (processing / "two.claimed").write_text("{}")
        lock_path = root / "dispatch" / "runner.lock"
        lock_path.touch()
        lock_fd = os.open(lock_path, os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        try:
            report = releases.preflight(root, pid_is_active=lambda _pid: True)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertFalse(report.ok)
        self.assertTrue(report.bootstrap)
        self.assertEqual(
            report.blockers,
            (
                "tracked seat claim: seat1",
                "tracked provider claim: claude",
                "active goal pid 111 (goal-a)",
                "active campaign pid 222 (campaign-a)",
                "dispatch inbox contains frame(s)",
                "dispatch processing contains frame(s)",
                "dispatch runner lock is active",
            ),
        )
        after = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_bootstrap_preflight_ignores_durable_changeset_records(self) -> None:
        root = self.temp / "root"
        for name in ("changesets", "applied", "dropped"):
            directory = root / "dispatch" / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "durable-record").write_text("not queued work")

        report = releases.preflight(root, pid_is_active=lambda _pid: False)

        self.assertTrue(report.ok)
        self.assertEqual(report.blockers, ())

    def test_activation_refuses_busy_bootstrap_and_incompatible_protocols(self) -> None:
        root = self.temp / "root"
        source = _source(self.temp)
        digest = _stage(root, source)
        (root / "seats.json").write_text(
            json.dumps({"seats": {"seat1": {"in_flight": {"dispatcher_pid": 1}}}})
        )

        with self.assertRaisesRegex(releases.ReleaseError, "tracked seat claim"):
            releases.activate(root, digest)
        self.assertFalse((root / "current").exists())

        (root / "seats.json").unlink()
        incompatible = _stage(
            root,
            source,
            "2",
            protocol_compatibility={
                **releases.DEFAULT_COMPATIBILITY,
                "dispatch": {
                    "min": releases.DISPATCH_PROTOCOL + 1,
                    "max": releases.DISPATCH_PROTOCOL + 1,
                },
            },
        )
        with self.assertRaisesRegex(
            releases.ReleaseError,
            rf"dispatch protocol {releases.DISPATCH_PROTOCOL}",
        ):
            releases.activate(root, incompatible)
        self.assertFalse((root / "current").exists())

    def test_activate_records_previous_and_rollback_only_swaps_pointers(self) -> None:
        root = self.temp / "root"
        source = _source(self.temp)
        first = _stage(root, source)
        self.assertEqual(
            releases.activate(root, first, confirm_heads_stopped=True),
            {"current": first, "previous": None, "changed": True},
        )

        (source / "lib" / "payload.txt").write_text("payload two\n")
        second = _stage(root, source, "2")
        self.assertEqual(
            releases.activate(root, second),
            {"current": second, "previous": first, "changed": True},
        )
        self.assertEqual(os.readlink(root / "current"), f"releases/{second}")
        self.assertEqual(os.readlink(root / "previous"), f"releases/{first}")

        # Active work is allowed after bootstrap: switching affects new work only.
        (root / "seats.json").write_text(
            json.dumps({"seats": {"seat1": {"in_flight": {"dispatcher_pid": 1}}}})
        )
        result = releases.rollback(root)
        self.assertEqual(
            result, {"current": first, "previous": second, "changed": True}
        )
        self.assertEqual(os.readlink(root / "current"), f"releases/{first}")
        self.assertEqual(os.readlink(root / "previous"), f"releases/{second}")
        self.assertTrue(
            json.loads((root / "seats.json").read_text())["seats"]["seat1"]["in_flight"]
        )

    def test_clean_bootstrap_still_requires_legacy_head_confirmation(self) -> None:
        root = self.temp / "root"
        source = _source(self.temp)
        digest = _stage(root, source)
        with self.assertRaisesRegex(
            releases.ReleaseError, "confirm-heads-stopped"
        ):
            releases.activate(root, digest)
        self.assertFalse((root / "current").exists())

    def test_cli_registers_every_command_and_status_is_read_only(self) -> None:
        parser = cli.build_parser()
        for action in (
            "stage",
            "verify",
            "preflight",
            "status",
            "activate",
            "install-shims",
            "rollback",
        ):
            argv = ["release", action]
            if action == "stage":
                argv.append("/source")
            elif action == "activate":
                argv.append("0" * 64)
            self.assertEqual(parser.parse_args(argv).handler, "release")

        root = self.temp / "missing-root"
        output = io.StringIO()
        with mock.patch.object(cli, "ROOT", root), redirect_stdout(output):
            self.assertEqual(cli.main(["release", "status"]), 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "bootstrap": True,
                "current": None,
                "previous": None,
                "releases": [],
            },
        )
        self.assertFalse(root.exists())

    def test_explicit_shim_install_is_drained_dry_runnable_and_release_pinned(
        self,
    ) -> None:
        root = self.temp / "root"
        source = _runtime_source(self.temp)
        first = _stage(root, source)
        releases.activate(root, first, confirm_heads_stopped=True)

        with self.assertRaisesRegex(releases.ReleaseError, "confirmation"):
            releases.install_bootstrap_shims(root, dry_run=True)
        dry_run = releases.install_bootstrap_shims(
            root,
            dry_run=True,
            confirm_heads_stopped=True,
        )
        self.assertEqual(dry_run["written"], [])
        self.assertFalse((root / "codex-seat").exists())

        installed = releases.install_bootstrap_shims(
            root,
            confirm_heads_stopped=True,
        )
        self.assertEqual(
            {Path(path).name for path in installed["written"]},
            {"codex-seat", "worker-hook"},
        )
        self.assertEqual(stat.S_IMODE((root / "codex-seat").stat().st_mode), 0o755)

        (source / "lib" / "codex_seats" / "cli.py").write_text(
            "import os\nprint('cli-new:' + os.environ['CODEX_SEATS_RELEASE_ID'])\n"
        )
        (source / "lib" / "codex_seats" / "worker_hook.py").write_text(
            "import os\nprint('hook-new:' + os.environ['CODEX_SEATS_RELEASE_ID'])\n"
        )
        second = _stage(root, source, "2")

        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        (bin_dir / "codex-seat").symlink_to(root / "codex-seat")
        cli_result = subprocess.run(
            [sys.executable, str(bin_dir / "codex-seat")],
            env={**os.environ, "CODEX_SEATS_RELEASE_ID": second},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(cli_result.returncode, 0)
        self.assertEqual(cli_result.stdout.strip(), f"cli:{first}")

        hook_result = subprocess.run(
            [sys.executable, str(root / "worker-hook")],
            env={**os.environ, "CODEX_SEATS_RELEASE_ID": second},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(hook_result.returncode, 0)
        self.assertEqual(hook_result.stdout.strip(), f"hook-new:{second}")
        invalid = subprocess.run(
            [sys.executable, str(root / "worker-hook")],
            env={**os.environ, "CODEX_SEATS_RELEASE_ID": "../escape"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(invalid.returncode, 126)


class ModuleRegressionTests(unittest.TestCase):
    """Activation must refuse a release that DROPS modules the active one ships.

    This is the failure that actually shipped: a release staged from the wrong source
    tree verified, preflighted, and activated cleanly while silently losing seven
    modules — releases.py among them, i.e. the code needed to undo it. Integrity
    attestation cannot catch that, so feature parity is checked separately.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)
        self.root = self.temp / "root"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _source_with(self, name: str, modules: tuple[str, ...]) -> Path:
        repo = self.temp / name
        package = repo / "lib" / "codex_seats"
        package.mkdir(parents=True)
        for module in modules:
            (package / module).write_text("")
        return repo

    def test_activate_refuses_a_release_that_drops_modules(self) -> None:
        full = _stage(self.root, self._source_with(
            "full", ("__init__.py", "cli.py", "releases.py", "task_policy.py")), "1")
        releases.activate(self.root, full, confirm_heads_stopped=True)

        crippled = _stage(self.root, self._source_with(
            "crippled", ("__init__.py", "cli.py")), "2")

        self.assertEqual(
            releases.module_regressions(self.root, crippled),
            ("releases.py", "task_policy.py"),
        )
        with self.assertRaises(releases.ReleaseError) as caught:
            releases.activate(self.root, crippled)
        self.assertIn("releases.py", str(caught.exception))
        # ...and the pointer did NOT move.
        self.assertEqual(releases.status(self.root)["current"], full)

        with self.assertRaisesRegex(releases.ReleaseError, "unapproved missing modules"):
            releases.activate(
                self.root,
                crippled,
                allow_module_removals=["releases.py"],
            )
        with self.assertRaisesRegex(releases.ReleaseError, "not missing"):
            releases.activate(
                self.root,
                crippled,
                allow_module_removals=["releases.py", "task_policy.py", "extra.py"],
            )
        releases.activate(
            self.root,
            crippled,
            allow_module_removals=["releases.py", "task_policy.py"],
        )
        self.assertEqual(releases.status(self.root)["current"], crippled)

    def test_module_removal_names_reject_paths_malformed_and_duplicates(self) -> None:
        for names in (
            ["../head_permissions.py"],
            ["package/head_permissions.py"],
            ["head-permissions.py"],
            ["head_permissions"],
            ["head_permissions.py", "head_permissions.py"],
        ):
            with self.subTest(names=names), self.assertRaises(releases.ReleaseError):
                releases.normalize_module_removals(names)

    def test_release_parser_replaces_broad_bypass_with_repeatable_exact_names(self) -> None:
        parser = cli.build_parser()
        parsed = parser.parse_args(
            [
                "release",
                "activate",
                "a" * 64,
                "--allow-module-removal",
                "one.py",
                "--allow-module-removal",
                "two.py",
            ]
        )
        self.assertEqual(parsed.allow_module_removal, ["one.py", "two.py"])
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["release", "activate", "a" * 64, "--allow-module-regressions"]
            )

    def test_additions_and_first_activation_are_not_regressions(self) -> None:
        base = _stage(self.root, self._source_with(
            "base", ("__init__.py", "cli.py")), "1")
        # No active release yet -> nothing to regress against.
        self.assertEqual(releases.module_regressions(self.root, base), ())
        releases.activate(self.root, base, confirm_heads_stopped=True)

        grown = _stage(self.root, self._source_with(
            "grown", ("__init__.py", "cli.py", "brand_new.py")), "2")
        self.assertEqual(releases.module_regressions(self.root, grown), ())
        releases.activate(self.root, grown)
        self.assertEqual(releases.status(self.root)["current"], grown)


if __name__ == "__main__":
    unittest.main()
