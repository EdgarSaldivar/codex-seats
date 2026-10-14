"""Transactional upgrade tests use only temporary roots and fake services."""

from __future__ import annotations

import fcntl
import io
import os
import plistlib
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from codex_seats import cli, launchd_dispatch, releases, upgrade

MODULES = (
    "__init__.py",
    "cli.py",
    "releases.py",
    "upgrade.py",
    "launchd_dispatch.py",
    "dispatch_runner.py",
)


def _publish(root: Path, base: Path, marker: str) -> str:
    source = base / f"source-{marker}"
    package = source / "lib" / "codex_seats"
    package.mkdir(parents=True)
    for name in MODULES:
        (package / name).write_text(f"# {marker} {name}\n", encoding="utf-8")
    bootstrap = source / "bootstrap"
    bootstrap.mkdir()
    for name in ("codex-seat", "worker-hook"):
        (bootstrap / name).write_bytes(
            b"#!/usr/bin/env python3\n# codex-seats immutable bootstrap shim v1\n"
            + marker.encode("ascii")
            + b"\n"
        )

    digit = (marker.encode("ascii").hex() + "0" * 40)[:40]

    def inspect(_source):
        return source, digit, digit

    def build(source_path, destination, _commit):
        for child in source_path.iterdir():
            if child.is_dir():
                shutil.copytree(child, destination / child.name)
            else:
                shutil.copy2(child, destination / child.name)

    return releases.stage_release(
        root, source, inspect_source=inspect, build_source=build
    )


class FakeDispatch:
    def __init__(self, plist: Path, *, loaded: bool = True) -> None:
        self.plist = plist
        self.loaded = loaded
        self.calls: list[str] = []
        self.bootstrap_failures = 0
        self.bootout_failure = False
        self.doctor_failure = False

    def operations(self) -> upgrade.DispatchOperations:
        return upgrade.DispatchOperations(
            plist_path=lambda: self.plist,
            render_bytes=self.render,
            status=self.status,
            bootout=self.bootout,
            bootstrap=self.bootstrap,
            doctor=self.doctor,
        )

    def render(self, root) -> bytes:
        return b"candidate-plist:" + releases.current_release_id(root).encode("ascii")

    def status(self, _root) -> dict:
        return {"installed": self.plist.exists(), "loaded": self.loaded}

    def bootout(self, _path) -> None:
        self.calls.append("bootout")
        if self.bootout_failure:
            raise RuntimeError("injected bootout failure")
        self.loaded = False

    def bootstrap(self, _path) -> None:
        self.calls.append("bootstrap")
        if self.bootstrap_failures:
            self.bootstrap_failures -= 1
            raise RuntimeError("injected bootstrap failure")
        self.loaded = True

    def doctor(self, root) -> list[tuple[bool, str]]:
        expected = self.render(root)
        good = not self.doctor_failure and self.plist.read_bytes() == expected
        return [(good, "fake dispatch doctor")]


class UpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "runtime"
        self.root.mkdir(mode=0o700)
        self.old = _publish(self.root, self.base, "a")
        self.candidate = _publish(self.root, self.base, "b")
        (self.root / "current").symlink_to(f"releases/{self.old}")
        (self.root / "previous").symlink_to(f"releases/{self.candidate}")
        (self.root / "codex-seat").write_bytes(b"old-cli")
        (self.root / "worker-hook").write_bytes(b"old-hook")
        os.chmod(self.root / "codex-seat", 0o751)
        os.chmod(self.root / "worker-hook", 0o750)
        self.plist = self.base / "LaunchAgents" / "dispatch.plist"
        self.plist.parent.mkdir()
        self.plist.write_bytes(b"old-plist")
        os.chmod(self.plist, 0o640)
        self.dispatch = FakeDispatch(self.plist)
        self.original = self._mutable_state()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _mutable_state(self) -> dict:
        result = {
            "current": os.readlink(self.root / "current")
            if (self.root / "current").is_symlink()
            else None,
            "previous": os.readlink(self.root / "previous")
            if (self.root / "previous").is_symlink()
            else None,
            "loaded": self.dispatch.loaded,
        }
        for name, path in (
            ("codex-seat", self.root / "codex-seat"),
            ("worker-hook", self.root / "worker-hook"),
            ("plist", self.plist),
        ):
            result[name] = (
                path.read_bytes() if path.exists() else None,
                stat.S_IMODE(path.stat().st_mode) if path.exists() else None,
            )
        return result

    def _run(self, **kwargs):
        candidate_verify = kwargs.pop(
            "candidate_verify", lambda _root, _candidate: None
        )
        with mock.patch.dict(os.environ, {"CODEX_SEATS_MANAGED_WORKER": "0"}):
            return upgrade.upgrade(
                self.root,
                self.base,
                stage=lambda _root, _source: self.candidate,
                smoke=lambda _root, _candidate: None,
                candidate_verify=candidate_verify,
                dispatch=self.dispatch.operations(),
                **kwargs,
            )

    def test_success_reconciles_every_artifact_and_removes_journal(self) -> None:
        result = self._run()
        self.assertTrue(result["ok"])
        self.assertEqual(releases.current_release_id(self.root), self.candidate)
        self.assertEqual(os.readlink(self.root / "previous"), f"releases/{self.old}")
        sources = releases._bootstrap_sources(self.root, self.candidate)
        self.assertEqual((self.root / "codex-seat").read_bytes(), sources["codex-seat"])
        self.assertEqual(
            (self.root / "worker-hook").read_bytes(), sources["worker-hook"]
        )
        self.assertEqual(self.plist.read_bytes(), self.dispatch.render(self.root))
        self.assertTrue(self.dispatch.loaded)
        self.assertFalse((self.root / upgrade.JOURNAL_NAME).exists())

    def test_upgrade_threads_exact_module_removal_authorization(self) -> None:
        source = self.base / "source-b"
        (source / "lib" / "codex_seats" / "dispatch_runner.py").unlink()

        def inspect(_source):
            return source, "c" * 40, "c" * 40

        def build(source_path, destination, _commit):
            for child in source_path.iterdir():
                if child.is_dir():
                    shutil.copytree(child, destination / child.name)
                else:
                    shutil.copy2(child, destination / child.name)

        self.candidate = releases.stage_release(
            self.root,
            source,
            inspect_source=inspect,
            build_source=build,
        )
        with self.assertRaisesRegex(upgrade.UpgradeError, "unapproved missing modules"):
            self._run()
        with self.assertRaisesRegex(upgrade.UpgradeError, "not missing"):
            self._run(
                allow_module_removals=["dispatch_runner.py", "extra.py"]
            )
        result = self._run(allow_module_removals=["dispatch_runner.py"])
        self.assertTrue(result["ok"])

    def test_drifted_active_release_fails_before_snapshot_or_mutation(self) -> None:
        active_file = (
            self.root
            / "releases"
            / self.old
            / "lib"
            / "codex_seats"
            / "cli.py"
        )
        os.chmod(active_file, 0o644)
        active_file.write_text("# drifted active release\n", encoding="utf-8")
        os.chmod(active_file, 0o444)

        with self.assertRaisesRegex(
            upgrade.UpgradeError, "active release failed verification"
        ):
            self._run()

        self.assertEqual(self._mutable_state(), self.original)
        self.assertFalse((self.root / upgrade.JOURNAL_NAME).exists())

    def test_noop_candidate_still_reconciles_and_rolls_back_on_failure(self) -> None:
        self.candidate = self.old
        original = self._mutable_state()
        self.dispatch.doctor_failure = True
        with self.assertRaises(upgrade.UpgradeError):
            self._run()
        self.assertEqual(self._mutable_state(), original)

    def test_every_mutating_phase_failure_restores_exact_snapshot(self) -> None:
        phases = (
            "activating-previous",
            "activating-current",
            "installing-shim-1",
            "installing-shim-2",
            "writing-plist",
            "booting-out-dispatch",
            "bootstrapping-dispatch",
            "verifying",
        )
        for phase in phases:
            with self.subTest(phase=phase):

                def fail(current, target=phase):
                    if current == target:
                        raise RuntimeError(f"injected {target}")

                with self.assertRaises(upgrade.UpgradeError) as caught:
                    self._run(checkpoint=fail)
                self.assertIn("rolled back", str(caught.exception))
                self.assertEqual(self._mutable_state(), self.original)
                self.assertFalse((self.root / upgrade.JOURNAL_NAME).exists())

    def test_stage_preflight_and_smoke_fail_before_snapshot_or_mutation(self) -> None:
        with (
            mock.patch.dict(os.environ, {"CODEX_SEATS_MANAGED_WORKER": "0"}),
            self.assertRaisesRegex(RuntimeError, "stage"),
        ):
            upgrade.upgrade(
                self.root,
                self.base,
                stage=lambda *_args: (_ for _ in ()).throw(RuntimeError("stage")),
                dispatch=self.dispatch.operations(),
            )
        self.assertEqual(self._mutable_state(), self.original)

        with (
            mock.patch.object(
                releases,
                "preflight",
                return_value=releases.PreflightReport(False, ("busy",)),
            ),
            self.assertRaisesRegex(upgrade.UpgradeError, "busy"),
        ):
            self._run()
        with (
            self.assertRaisesRegex(upgrade.UpgradeError, "smoke"),
            mock.patch.dict(os.environ, {"CODEX_SEATS_MANAGED_WORKER": "0"}),
        ):
            upgrade.upgrade(
                self.root,
                self.base,
                stage=lambda *_args: self.candidate,
                smoke=lambda *_args: (_ for _ in ()).throw(
                    upgrade.UpgradeError("smoke")
                ),
                dispatch=self.dispatch.operations(),
            )
        self.assertEqual(self._mutable_state(), self.original)
        self.assertFalse((self.root / upgrade.JOURNAL_NAME).exists())

    def test_routine_upgrade_requires_dispatch_and_claims_to_be_drained(self) -> None:
        inbox = self.root / "dispatch" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "queued.ready").write_text("queued")
        with self.assertRaisesRegex(upgrade.UpgradeError, "dispatch inbox"):
            self._run()
        self.assertEqual(self._mutable_state(), self.original)
        self.assertFalse((self.root / upgrade.JOURNAL_NAME).exists())

    def test_bootstrap_failure_reboots_exact_old_loaded_plist(self) -> None:
        self.dispatch.bootstrap_failures = 1
        with self.assertRaises(upgrade.UpgradeError):
            self._run()
        self.assertEqual(self._mutable_state(), self.original)
        self.assertEqual(self.dispatch.calls[-2:], ["bootout", "bootstrap"])

    def test_absent_unloaded_dispatch_is_absent_unloaded_after_rollback(self) -> None:
        self.plist.unlink()
        self.dispatch.loaded = False
        original = self._mutable_state()
        self.dispatch.doctor_failure = True
        with self.assertRaises(upgrade.UpgradeError):
            self._run()
        self.assertEqual(self._mutable_state(), original)

    def test_final_doctor_and_rollback_failure_reports_both_and_keeps_journal(
        self,
    ) -> None:
        self.dispatch.doctor_failure = True
        self.dispatch.bootout_failure = True
        with self.assertRaises(upgrade.UpgradeError) as caught:
            self._run()
        self.assertIn("rollback also failed", str(caught.exception))
        self.assertIn("primary_error", caught.exception.result)
        self.assertIn("rollback_error", caught.exception.result)
        self.assertTrue((self.root / upgrade.JOURNAL_NAME).exists())

    def test_candidate_final_verifier_failure_rolls_back(self) -> None:
        with self.assertRaisesRegex(upgrade.UpgradeError, "candidate-final"):
            self._run(
                candidate_verify=lambda *_args: (_ for _ in ()).throw(
                    upgrade.UpgradeError("candidate-final")
                )
            )
        self.assertEqual(self._mutable_state(), self.original)

    def test_process_death_is_explicitly_recoverable_without_launchctl(self) -> None:
        def die(phase):
            if phase == "installing-shim-2":
                raise KeyboardInterrupt("simulated process death")

        with self.assertRaises(KeyboardInterrupt):
            self._run(checkpoint=die)
        status = upgrade.upgrade_status(self.root)
        self.assertTrue(status["incomplete"])
        with mock.patch.dict(os.environ, {"CODEX_SEATS_MANAGED_WORKER": "0"}):
            result = upgrade.recover(self.root, dispatch=self.dispatch.operations())
        self.assertTrue(result["restored"])
        self.assertEqual(self.dispatch.calls, [])
        self.assertEqual(self._mutable_state(), self.original)
        self.assertFalse(upgrade.upgrade_status(self.root)["incomplete"])

    def test_recovery_fails_closed_on_unrelated_newer_value(self) -> None:
        def die(phase):
            if phase == "writing-plist":
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self._run(checkpoint=die)
        (self.root / "codex-seat").write_bytes(b"unrelated-newer")
        with (
            mock.patch.dict(os.environ, {"CODEX_SEATS_MANAGED_WORKER": "0"}),
            self.assertRaisesRegex(upgrade.UpgradeError, "ownership check"),
        ):
            upgrade.recover(self.root, dispatch=self.dispatch.operations())
        self.assertEqual((self.root / "codex-seat").read_bytes(), b"unrelated-newer")
        self.assertTrue((self.root / upgrade.JOURNAL_NAME).exists())

    def test_lock_is_bounded_and_second_upgrade_refuses(self) -> None:
        lock = self.root / upgrade.LOCK_NAME
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaisesRegex(upgrade.UpgradeError, "busy"):
                self._run(lock_timeout=0.01)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_managed_worker_refuses_upgrade_and_recovery_mutations(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_SEATS_MANAGED_WORKER": "1"}):
            with self.assertRaisesRegex(upgrade.UpgradeError, "managed workers"):
                upgrade.upgrade(
                    self.root, self.base, dispatch=self.dispatch.operations()
                )
            with self.assertRaisesRegex(upgrade.UpgradeError, "managed workers"):
                upgrade.recover(self.root, dispatch=self.dispatch.operations())

    def test_cli_registers_upgrade_family_and_returns_nonzero_refusal(self) -> None:
        parser = cli.build_parser()
        for argv in (
            ["release", "upgrade", str(self.base)],
            ["release", "upgrade-status"],
            ["release", "upgrade-recover"],
        ):
            self.assertEqual(parser.parse_args(argv).handler, "release")
        args = parser.parse_args(["release", "upgrade", str(self.base)])
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"CODEX_SEATS_MANAGED_WORKER": "1"}),
            redirect_stderr(stderr),
        ):
            self.assertEqual(releases.cmd_release(args, self.root), 1)
        self.assertIn("managed workers", stderr.getvalue())

        status_args = parser.parse_args(["release", "upgrade-status"])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(releases.cmd_release(status_args, self.root), 0)
        self.assertIn('"incomplete": false', stdout.getvalue())

    def test_dispatch_snapshot_rejects_unknown_environment_payload(self) -> None:
        payload = launchd_dispatch.render_plist(self.root)
        payload["EnvironmentVariables"]["SECRET_TOKEN"] = "must-not-be-journaled"
        with self.assertRaisesRegex(upgrade.UpgradeError, "environment"):
            upgrade._validate_dispatch_snapshot(self.root, plistlib.dumps(payload))

    def test_dispatch_snapshot_accepts_actual_canonical_plist(self) -> None:
        payload = plistlib.dumps(launchd_dispatch.render_plist(self.root))
        upgrade._validate_dispatch_snapshot(self.root, payload)

    def test_symlink_and_oversize_artifacts_fail_before_journal(self) -> None:
        (self.root / "codex-seat").unlink()
        (self.root / "codex-seat").symlink_to(self.root / "worker-hook")
        with self.assertRaisesRegex(upgrade.UpgradeError, "regular file"):
            self._run()
        self.assertFalse((self.root / upgrade.JOURNAL_NAME).exists())

        (self.root / "codex-seat").unlink()
        (self.root / "codex-seat").write_bytes(b"x" * (upgrade.MAX_ARTIFACT_BYTES + 1))
        with self.assertRaisesRegex(upgrade.UpgradeError, "size limit"):
            self._run()

    def test_non_owner_artifact_is_rejected(self) -> None:
        with (
            mock.patch.object(upgrade.os, "getuid", return_value=os.getuid() + 1),
            self.assertRaisesRegex(upgrade.UpgradeError, "owner-controlled"),
        ):
            upgrade._snapshot(self.root / "codex-seat")

    def test_symlink_and_oversize_journals_are_rejected_read_only(self) -> None:
        journal = self.root / upgrade.JOURNAL_NAME
        journal.symlink_to(self.root / "codex-seat")
        with self.assertRaises(upgrade.UpgradeError):
            upgrade.upgrade_status(self.root)
        journal.unlink()
        journal.write_bytes(b"x" * (upgrade.MAX_JOURNAL_BYTES + 1))
        os.chmod(journal, 0o600)
        with self.assertRaisesRegex(upgrade.UpgradeError, "size limit"):
            upgrade.upgrade_status(self.root)


if __name__ == "__main__":
    unittest.main()
