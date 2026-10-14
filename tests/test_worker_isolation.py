from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from codex_seats.claude_isolation import claude_job_settings
from codex_seats.provider_claude import run_claude
from registry_fixture import registry_settings
from codex_seats.scope import effective_repository_write_scope, worker_permission_profile
from codex_seats.worker_hook import worker_decision
from codex_seats import internal_git
from codex_seats.write_boundary import (
    GitMetadataError,
    boundary_plan,
    configured_seat_home_paths,
    git_metadata_paths,
    known_credential_store_paths,
)


def _git_repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(
        ["git", "-C", str(path), "init", "-q"],
        stdin=subprocess.DEVNULL,
        check=True,
    )
    return path.resolve()


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


def _commit_fixture(path: Path, filename: str = "fixture.txt") -> None:
    (path / filename).write_text("fixture\n")
    _git(path, "add", filename)
    _git(
        path,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture",
    )


class BalancedBoundaryTests(unittest.TestCase):
    def test_dynamic_schema3_homes_and_control_artifacts_are_denied(self):
        import tomllib

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            base = Path(directory).resolve()
            root = base / "control"
            repo = base / "repo"
            repo.mkdir()
            private = root / "private-tmp" / "assigned"
            external = base / "external-seat-home"
            private.mkdir(parents=True)
            external.mkdir()
            hook = base / "hook"
            hook.write_text("")
            settings = registry_settings()
            entry = {
                "label": "Seat",
                "lifecycle": "inactive",
                "home": None,
                "capacity_profile": "personal-plus",
                "relative_capacity": None,
                "capacity_source": "unverified",
                "capacity_verified_at": None,
            }
            settings["pool"]["seats"] = {
                "seat6": dict(entry),
                "seat8": {**entry, "home": str(external)},
            }

            native = tomllib.loads(
                worker_permission_profile(
                    "job",
                    repo,
                    [{"path": ".", "type": "tree"}],
                    [],
                    private,
                    control_root=root,
                    settings=settings,
                )
            )["permissions"]["job"]["filesystem"]
            for target in (
                root / ".*",
                root / "seat*",
                root / "seat6",
                external,
                root / "settings.json.backup-*",
                root / ".settings.lock",
                root / ".settings-readers.lock",
                root / "readers",
                root / "seat-admin-audit.jsonl",
                root / "archive",
                root / "releases",
                root / "current",
                root / "previous",
                root / "shipping",
                root / "upgrade-transaction.json",
                root / "upgrade.lock",
            ):
                self.assertEqual(native[str(target)], "deny", target)
            self.assertEqual(native[str(private)], "write")
            self.assertEqual(native[str(repo)], "write")

            claude = claude_job_settings(
                repo,
                [{"path": ".", "type": "tree"}],
                [],
                private,
                hook,
                control_root=root,
                settings=settings,
            )
            filesystem = claude["sandbox"]["filesystem"]
            self.assertIn(str(root / "seat6"), filesystem["denyRead"])
            self.assertIn(str(external), filesystem["denyRead"])
            self.assertIn(str(root / "shipping"), filesystem["denyWrite"])
            self.assertIn(str(private), filesystem["allowWrite"])
            tool_denies = claude["permissions"]["deny"]
            for target in (root / "seat6", external, root / "upgrade.lock"):
                tool_target = f"/{target}"
                self.assertIn(f"Read({tool_target})", tool_denies)
                self.assertIn(f"Edit({tool_target}/**)", tool_denies)
                self.assertIn(f"Write({tool_target}/**)", tool_denies)

            environment = {
                "CODEX_SEATS_MANAGED_WORKER": "1",
                "CODEX_SEATS_TASK_ROOT": str(repo),
                "CODEX_SEATS_CONTROL_ROOT": str(root),
                "CODEX_SEATS_PRIVATE_TEMP": str(private),
                "CODEX_SEATS_PROTECTED_HOMES": json.dumps(
                    [str(root / "seat*"), str(root / "seat6"), str(external)]
                ),
                "CODEX_SEATS_WRITE_SCOPE": json.dumps(
                    [{"path": ".", "type": "tree"}]
                ),
                "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
                "CODEX_SEATS_PROTECTED_PATHS": "[]",
            }
            for target in (
                root / "seat6" / "config.toml",
                root / "seat12" / "config.toml",
                external / "config.toml",
                root / "settings.json.backup-sha256-fixture",
                root / ".future-control-plane.lock",
                root / ".settings-readers.lock",
                root / "shipping" / "audit.jsonl",
            ):
                decision = worker_decision(
                    {
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(target)},
                    },
                    environment,
                )
                self.assertEqual(decision["decision"], "block", target)
            self.assertIsNone(
                worker_decision(
                    {
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(private / "result.json")},
                    },
                    environment,
                )
            )
    def test_known_store_and_seat_home_aliases_keep_both_variants(self):
        import tomllib

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            base = Path(directory).resolve()
            root = base / "control"
            repo = base / "repo"
            repo.mkdir()
            private = root / "private-tmp" / "assigned"
            private.mkdir(parents=True)
            hook = base / "hook"
            hook.write_text("")
            home = base / "home"
            home.mkdir()
            store = base / "writable" / "aws-store"
            store.mkdir(parents=True)
            (home / ".aws").symlink_to(store, target_is_directory=True)
            stores = known_credential_store_paths(home)
            self.assertIn(home / ".aws", stores)
            self.assertIn(store, stores)
            # Wildcard store patterns are never resolved as literal files.
            self.assertEqual(
                [path for path in stores if "seat*" in str(path)],
                [home / ".codex-seats" / "seat*" / ("auth" + ".json")],
            )

            seat_target = base / "writable" / "seat-home-target"
            seat_target.mkdir()
            seat_alias = base / "writable" / "seat-home-alias"
            seat_alias.symlink_to(seat_target, target_is_directory=True)
            settings = registry_settings()
            settings["pool"]["seats"] = {
                "seat9": {
                    "label": "Seat",
                    "lifecycle": "active",
                    "home": str(seat_alias),
                }
            }
            homes = configured_seat_home_paths(root, settings)
            self.assertIn(seat_alias, homes)
            self.assertIn(seat_target, homes)
            self.assertIn(root / "seat*", homes)

            native = tomllib.loads(
                worker_permission_profile(
                    "job",
                    repo,
                    [{"path": ".", "type": "tree"}],
                    [],
                    private,
                    home=home,
                    control_root=root,
                    settings=settings,
                )
            )["permissions"]["job"]["filesystem"]
            for target in (home / ".aws", store, seat_alias, seat_target):
                self.assertEqual(native[str(target)], "deny", target)

            claude = claude_job_settings(
                repo,
                [{"path": ".", "type": "tree"}],
                [],
                private,
                hook,
                control_root=root,
                settings=settings,
            )
            filesystem = claude["sandbox"]["filesystem"]
            for target in (seat_alias, seat_target):
                self.assertIn(str(target), filesystem["denyRead"])
                self.assertIn(str(target), filesystem["denyWrite"])
                self.assertIn(
                    f"Write(/{target}/**)", claude["permissions"]["deny"]
                )

            environment = {
                "CODEX_SEATS_MANAGED_WORKER": "1",
                "CODEX_SEATS_TASK_ROOT": str(repo),
                "CODEX_SEATS_CONTROL_ROOT": str(root),
                "CODEX_SEATS_WRITE_SCOPE": json.dumps([{"path": ".", "type": "tree"}]),
                "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
                "CODEX_SEATS_PROTECTED_PATHS": "[]",
                "CODEX_SEATS_PROTECTED_HOMES": json.dumps(
                    [str(path) for path in homes]
                ),
            }
            for target in (seat_alias / "config.toml", seat_target / "config.toml"):
                decision = worker_decision(
                    {"tool_name": "Write", "tool_input": {"file_path": str(target)}},
                    environment,
                )
                self.assertEqual(decision["decision"], "block", target)

    def test_serial_mutation_balances_while_strict_parallel_and_readonly_stay_exact(self):
        declared = [{"path": "src", "type": "tree"}]
        self.assertEqual(
            effective_repository_write_scope(declared),
            [{"path": ".", "type": "tree"}],
        )
        self.assertEqual(
            effective_repository_write_scope(declared, strict_scope=True), declared
        )
        self.assertEqual(
            effective_repository_write_scope(declared, parallel_safe=True), declared
        )
        self.assertEqual(effective_repository_write_scope([]), [])
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            balanced = boundary_plan(repo, declared, [])
            strict = boundary_plan(repo, declared, [], strict_scope=True)
            parallel = boundary_plan(repo, declared, [], parallel_safe=True)
            readonly = boundary_plan(repo, [], [])
            readonly_git = boundary_plan(repo, [], [], allow_git_write=True)
            balanced_git = boundary_plan(
                repo, declared, [], allow_git_write=True
            )
            external_only = boundary_plan(
                repo,
                [],
                [{"path": str(Path(directory) / "external"), "type": "tree"}],
            )
        self.assertEqual(balanced.mode, "balanced")
        self.assertEqual(strict.mode, "exact")
        self.assertEqual(parallel.mode, "exact")
        self.assertEqual(readonly.mode, "read-only")
        self.assertFalse(readonly_git.allow_git_write)
        self.assertFalse(balanced_git.allow_git_write)
        self.assertEqual(external_only.mode, "exact")

    def test_native_profiles_keep_controls_and_git_denied_despite_legacy_flag(self):
        import tomllib

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = _git_repository(root / "repo")
            (repo / "src").mkdir()
            private = root / "private-tmp" / "job"
            private.mkdir(parents=True)
            denied = tomllib.loads(
                worker_permission_profile(
                    "job",
                    repo,
                    [{"path": ".", "type": "tree"}],
                    [],
                    private,
                    control_root=root,
                )
            )["permissions"]["job"]["filesystem"]
            self.assertEqual(denied[str(repo / ".git")], "read")
            self.assertEqual(denied[str(root / "dispatch")], "deny")
            self.assertEqual(denied[str(root / "settings.json")], "deny")
            self.assertEqual(denied[str(root / "private-tmp")], "deny")
            self.assertEqual(denied[str(root / "worker-launches")], "deny")
            self.assertEqual(denied[str(private)], "write")
            attempted_override = tomllib.loads(
                worker_permission_profile(
                    "job",
                    repo,
                    [{"path": "src", "type": "tree"}],
                    [],
                    private,
                    allow_git_write=True,
                    control_root=root,
                )
            )["permissions"]["job"]["filesystem"]
            self.assertEqual(attempted_override[str(repo)], "read")
            self.assertEqual(attempted_override[str(repo / "src")], "write")
            self.assertEqual(attempted_override[str(repo / ".git")], "read")
            self.assertEqual(attempted_override[str(root / "dispatch")], "deny")
            read_only = tomllib.loads(
                worker_permission_profile(
                    "job",
                    repo,
                    [],
                    [],
                    private,
                    generic_temp=root,
                    control_root=root,
                )
            )["permissions"]["job"]["filesystem"]
            self.assertEqual(read_only[str(root)], "deny")
            self.assertEqual(read_only[str(repo)], "read")
            self.assertEqual(read_only[str(repo / ".git")], "read")

    def test_linked_worktree_common_and_recursive_submodule_metadata_are_denied(self):
        import tomllib

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            submodule_origin = _git_repository(root / "submodule-origin")
            _commit_fixture(submodule_origin, "submodule.txt")
            primary = _git_repository(root / "primary")
            _commit_fixture(primary)
            submodule_commit = _git(submodule_origin, "rev-parse", "HEAD")
            (primary / ".gitmodules").write_text(
                '[submodule "Dependencies/Fixture"]\n'
                "\tpath = Dependencies/Fixture\n"
                f"\turl = {submodule_origin}\n"
            )
            _git(primary, "add", ".gitmodules")
            _git(
                primary,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{submodule_commit},Dependencies/Fixture",
            )
            _git(
                primary,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-q",
                "-m",
                "add submodule",
            )
            linked = root / "linked"
            _git(primary, "worktree", "add", "-q", "-b", "linked", str(linked))
            _git(
                linked,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "update",
                "--init",
                "-q",
            )
            submodule = linked / "Dependencies" / "Fixture"
            admin = Path(_git(linked, "rev-parse", "--absolute-git-dir")).resolve()
            common = Path(
                _git(linked, "rev-parse", "--path-format=absolute", "--git-common-dir")
            ).resolve()
            submodule_admin = Path(
                _git(submodule, "rev-parse", "--absolute-git-dir")
            ).resolve()
            submodule_common = Path(
                _git(
                    submodule,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                )
            ).resolve()
            metadata = tuple(git_metadata_paths(linked))
            for target in (
                linked / ".git",
                admin,
                common,
                submodule / ".git",
                submodule_admin,
                submodule_common,
            ):
                self.assertIn(target, metadata)

            private = root / "private"
            private.mkdir()
            codex = tomllib.loads(
                worker_permission_profile(
                    "job",
                    linked,
                    [{"path": ".", "type": "tree"}],
                    [],
                    private,
                    allow_git_write=True,
                    git_metadata=metadata,
                    control_root=root / "control",
                )
            )["permissions"]["job"]["filesystem"]
            self.assertEqual(codex[str(linked)], "write")
            for target in metadata:
                self.assertEqual(codex[str(target)], "read", target)

            hook = root / "worker-hook"
            hook.write_text("")
            claude = claude_job_settings(
                linked,
                [{"path": ".", "type": "tree"}],
                [],
                private,
                hook,
                allow_git_write=True,
                git_metadata=metadata,
                control_root=root / "control",
            )
            filesystem = claude["sandbox"]["filesystem"]
            self.assertIn(str(linked), filesystem["allowWrite"])
            native_denies = claude["permissions"]["deny"]
            for target in metadata:
                self.assertIn(str(target), filesystem["denyWrite"], target)
                exact = f"/{target}"
                self.assertIn(f"Edit({exact})", native_denies, target)
                self.assertIn(f"Write({exact}/**)", native_denies, target)

            hook_env = {
                "CODEX_SEATS_MANAGED_WORKER": "1",
                "CODEX_SEATS_TASK_ROOT": str(linked),
                "CODEX_SEATS_CONTROL_ROOT": str(root / "control"),
                "CODEX_SEATS_PRIVATE_TEMP": str(private),
                "CODEX_SEATS_WRITE_SCOPE": json.dumps(
                    [{"path": ".", "type": "tree"}]
                ),
                "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
                "CODEX_SEATS_PROTECTED_PATHS": "[]",
                "CODEX_SEATS_ALLOW_GIT_WRITE": "1",
                "CODEX_SEATS_GIT_METADATA_PATHS": json.dumps(
                    [str(path) for path in metadata]
                ),
            }
            for target in (common / "info" / "exclude", submodule_admin / "config"):
                decision = worker_decision(
                    {
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(target)},
                    },
                    hook_env,
                )
                self.assertEqual(decision["decision"], "block", target)
            patch_decision = worker_decision(
                {
                    "tool_name": "apply_patch",
                    "tool_input": {
                        "patch": (
                            "*** Begin Patch\n"
                            f"*** Update File: {common / 'info' / 'exclude'}\n"
                            "@@\n+blocked\n*** End Patch"
                        )
                    },
                },
                hook_env,
            )
            self.assertEqual(patch_decision["decision"], "block")

    def test_git_metadata_discovery_fails_closed_but_non_git_scratch_works(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory).resolve()
            boundary = boundary_plan(
                scratch, [{"path": ".", "type": "tree"}], []
            )
            self.assertEqual(
                boundary.git_metadata,
                (scratch / ".git", scratch / ".git-meta"),
            )
            failure = internal_git.GitLaunchError("missing Git", ("git",))
            with mock.patch(
                "codex_seats.write_boundary.internal_git.run", side_effect=failure
            ):
                with self.assertRaises(GitMetadataError):
                    boundary_plan(scratch, [{"path": ".", "type": "tree"}], [])

    def test_claude_native_settings_have_no_broad_directory_and_deny_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = _git_repository(root / "repo")
            scope = repo / "src"
            scope.mkdir()
            private = root / "private"
            private.mkdir()
            hook = root / "hook"
            hook.write_text("")
            settings = claude_job_settings(
                repo,
                [{"path": ".", "type": "tree"}],
                [],
                private,
                hook,
                control_root=root,
            )
            self.assertEqual(settings["permissions"]["additionalDirectories"], [])
            filesystem = settings["sandbox"]["filesystem"]
            self.assertIn(str(root / "dispatch"), filesystem["denyWrite"])
            self.assertIn(str(root / "settings.json"), filesystem["denyWrite"])
            self.assertIn(str(root / "worker-launches"), filesystem["denyWrite"])
            self.assertIn(str(root / "worker-launches"), filesystem["denyRead"])
            self.assertIn(str(repo / ".git"), filesystem["denyWrite"])
            self.assertEqual(settings["enabledPlugins"], {})
            self.assertTrue(settings["disableAgents"])

    def test_claude_private_temp_snapshot_denies_peers_and_allows_assigned_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = _git_repository(root / "repo")
            private = root / "private-tmp" / "job-one"
            sibling = root / "private-tmp" / "job-two"
            private.mkdir(parents=True)
            sibling.mkdir()
            hook = root / "worker-hook"
            hook.write_text("")

            settings = claude_job_settings(
                repo, [], [], private, hook, control_root=root
            )
            allow = settings["permissions"]["allow"]
            deny = settings["permissions"]["deny"]
            filesystem = settings["sandbox"]["filesystem"]
            private_parent = str(root / "private-tmp")

            # A parent deny would also hide the assigned child in Claude.  The
            # compensating boundary is a static peer snapshot plus worker_hook.
            self.assertNotIn(private_parent, filesystem["denyRead"])
            self.assertIn(str(sibling), filesystem["denyRead"])
            self.assertIn(str(sibling), filesystem["denyWrite"])
            self.assertIn(f"Read(/{private}/**)", allow)
            self.assertIn(f"Read(/{sibling}/**)", deny)
            self.assertNotIn(f"Read(/{private}/**)", deny)

            future_sibling = root / "private-tmp" / "job-created-later"
            future_sibling.mkdir()
            self.assertNotIn(str(future_sibling), filesystem["denyRead"])
            future_decision = worker_decision(
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": str(future_sibling / "result.json")},
                },
                {
                    "CODEX_SEATS_MANAGED_WORKER": "1",
                    "CODEX_SEATS_TASK_ROOT": str(repo),
                    "CODEX_SEATS_CONTROL_ROOT": str(root),
                    "CODEX_SEATS_PRIVATE_TEMP": str(private),
                },
            )
            self.assertEqual(future_decision["decision"], "block")


class ClaudeCwdAndFallbackTests(unittest.TestCase):
    def test_boundary_discovery_failure_releases_claim_without_launch(self):
        from codex_seats.state import Ledger

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = _git_repository(root / "repo")
            with mock.patch(
                "codex_seats.provider_claude.boundary_plan",
                side_effect=GitMetadataError("synthetic discovery failure"),
            ):
                result, calls = self._run(root, repo)
            self.assertNotEqual(result.status, "completed")
            self.assertEqual(calls, [])
            with Ledger(root) as data:
                self.assertIsNone(data["providers"]["claude"]["in_flight"])

    def _settings(self) -> dict[str, object]:
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "manual_only"
        settings["providers"]["claude"]["model_roles"] = {
            "frontier": "fable",
            "complex": "opus",
            "routine": "sonnet",
        }
        return settings

    def _run(
        self,
        root: Path,
        repo: Path,
        *,
        strict: bool = False,
        parallel: bool = False,
        readonly: bool = False,
        execute=None,
    ):
        calls: list[tuple[list[str], str, dict[str, str]]] = []

        def successful(command, output, *_args, **_kwargs):
            calls.append((command, _args[1], _args[2]))
            payload = {
                "type": "result",
                "subtype": "success",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "structured_output": {
                    "status": "completed",
                    "summary": "done",
                    "changed_files": [],
                    "test_evidence": ["fixture"],
                    "warnings": [],
                    "needs_controller": None,
                },
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(payload))
            return "completed", json.dumps(payload), 0, 1

        callback = execute or successful

        def recording(command, output, *_args, **_kwargs):
            if execute is not None:
                calls.append((command, _args[1], _args[2]))
            return callback(command, output, *_args, **_kwargs)

        request = {
            "prompt": "work",
            "kind": "docs",
            "size": "small",
            "_capacity_checked": True,
            "cd": str(repo),
            "write_scope": [] if readonly else [{"path": "src", "type": "tree"}],
            "external_write_scope": [],
            "sandbox": "read-only" if readonly else "workspace-write",
            "strict_scope": strict,
            "parallel_safe": parallel,
        }
        with (
            mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/claude"),
            mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
            mock.patch("codex_seats.workers._execute", side_effect=recording),
        ):
            result = run_claude(root, self._settings(), request)
        return result, calls

    def test_balanced_uses_repo_cwd_and_exact_modes_use_private_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = _git_repository(root / "repo")
            (repo / "src").mkdir()
            balanced, calls = self._run(root, repo)
            self.assertEqual(balanced.status, "completed")
            self.assertEqual(calls[-1][1], str(repo))
            self.assertFalse(Path(calls[-1][2]["CODEX_SEATS_PRIVATE_TEMP"]).exists())
            self.assertEqual(list((root / "worker-launches").iterdir()), [])
            self.assertEqual(json.loads(calls[-1][2]["CODEX_SEATS_WRITE_SCOPE"]), [{"path": ".", "type": "tree"}])
            for kwargs in (
                {"strict": True},
                {"parallel": True},
                {"readonly": True},
            ):
                result, exact_calls = self._run(root, repo, **kwargs)
                self.assertEqual(result.status, "completed")
                self.assertNotEqual(exact_calls[-1][1], str(repo))
                self.assertEqual(Path(exact_calls[-1][1]).parent, root / "private-tmp")

    def test_timeout_parse_failure_and_contained_change_all_fall_back_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = _git_repository(root / "repo")
            (repo / "src").mkdir()
            target = repo / "src" / "work.txt"
            target.write_text("base")

            def timed_out(_command, output, *_args, **_kwargs):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({"type": "error", "result": "timed out"}))
                return "timeout", "timed out", 124, 1

            safe, _calls = self._run(root, repo, execute=timed_out)
            self.assertEqual(safe.status, "capacity")
            self.assertTrue(safe.fallback_safe)

            def malformed(_command, output, *_args, **_kwargs):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({"type": "result", "result": "no schema"}))
                return "completed", "no schema", 0, 1

            parse_safe, _calls = self._run(root, repo, execute=malformed)
            self.assertEqual(parse_safe.status, "capacity")
            self.assertTrue(parse_safe.fallback_safe)

            # A failed attempt that also changed files is contained in the disposable
            # worktree, so it too is safe to fall back — no blocked_needs_controller.
            def changed_then_failed(_command, output, *_args, **_kwargs):
                target.write_text("partial")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({"type": "error", "result": "failed"}))
                return "failed", "failed", 1, 1

            contained, _calls = self._run(root, repo, execute=changed_then_failed)
            self.assertEqual(contained.status, "capacity")
            self.assertTrue(contained.fallback_safe)


class WorkerDefenseTests(unittest.TestCase):
    def test_status_is_read_only_but_pool_control_writes_remain_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = {
                "CODEX_SEATS_MANAGED_WORKER": "1",
                "CODEX_SEATS_TASK_ROOT": str(root),
                "CODEX_SEATS_CONTROL_ROOT": str(root),
                "CODEX_SEATS_WRITE_SCOPE": json.dumps([{"path": ".", "type": "tree"}]),
                "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
                "CODEX_SEATS_PROTECTED_PATHS": "[]",
            }
            self.assertIsNone(
                worker_decision(
                    {
                        "tool_name": "Bash",
                        "tool_input": {"command": "codex-seat status --json"},
                    },
                    env,
                )
            )
            for target in (
                "seats.json",
                ".ledger.lock",
                "settings.json",
                "worker-launches/job/settings.json",
            ):
                denied = worker_decision(
                    {
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(root / target)},
                    },
                    env,
                )
                self.assertEqual(denied["decision"], "block", target)

    def test_pool_controls_are_denied_even_inside_balanced_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = {
                "CODEX_SEATS_MANAGED_WORKER": "1",
                "CODEX_SEATS_TASK_ROOT": str(root),
                "CODEX_SEATS_CONTROL_ROOT": str(root),
                "CODEX_SEATS_WRITE_SCOPE": json.dumps([{"path": ".", "type": "tree"}]),
                "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
                "CODEX_SEATS_PROTECTED_PATHS": "[]",
            }
            denied = worker_decision(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(root / "dispatch" / "x")},
                },
                env,
            )
            self.assertEqual(denied["decision"], "block")
            allowed = worker_decision(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(root / "src.py")},
                },
                env,
            )
            self.assertIsNone(allowed)

    def test_dispatch_controls_are_denied_to_workers(self):
        # A worker must never touch the dispatch queue (forge receipts / self-delegate)
        # nor operate the runner LaunchAgent — those belong to heads and the bridge.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = {
                "CODEX_SEATS_MANAGED_WORKER": "1",
                "CODEX_SEATS_TASK_ROOT": str(root),
                "CODEX_SEATS_CONTROL_ROOT": str(root),
                "CODEX_SEATS_WRITE_SCOPE": json.dumps([{"path": ".", "type": "tree"}]),
                "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
                "CODEX_SEATS_PROTECTED_PATHS": "[]",
            }
            for target in ("dispatch/inbox/x.ready", "dispatch/receipts/y.json"):
                blocked = worker_decision(
                    {"tool_name": "Write", "tool_input": {"file_path": str(root / target)}},
                    env,
                )
                self.assertEqual(blocked["decision"], "block", target)
            cmd = worker_decision(
                {"tool_name": "Bash", "tool_input": {"command": "codex-seat dispatch install"}},
                env,
            )
            self.assertEqual(cmd["decision"], "block")
            for command in (
                "launchctl bootout gui/501/io.codex-seats.dispatch",
                "launchctl bootstrap gui/501 ~/Library/LaunchAgents/io.codex-seats.dispatch.plist",
                "env launchctl kickstart -k gui/501/io.codex-seats.dispatch",
                "sh -c 'launchctl bootout gui/501/io.codex-seats.dispatch'",
            ):
                with self.subTest(command=command):
                    blocked = worker_decision(
                        {"tool_name": "Bash", "tool_input": {"command": command}},
                        env,
                    )
                    self.assertEqual(blocked["decision"], "block")
            allowed = worker_decision(
                {"tool_name": "Write", "tool_input": {"file_path": str(root / "src.py")}},
                env,
            )
            self.assertIsNone(allowed)

    def test_only_the_current_private_temp_is_exempt_from_runtime_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            private = root / "private-tmp" / "job-one"
            sibling = root / "private-tmp" / "job-two"
            private.mkdir(parents=True)
            sibling.mkdir()
            env = {
                "CODEX_SEATS_MANAGED_WORKER": "1",
                "CODEX_SEATS_TASK_ROOT": str(root),
                "CODEX_SEATS_CONTROL_ROOT": str(root),
                "CODEX_SEATS_PRIVATE_TEMP": str(private),
                "CODEX_SEATS_WRITE_SCOPE": "[]",
                "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
                "CODEX_SEATS_PROTECTED_PATHS": "[]",
            }
            own = worker_decision(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(private / "result.json")},
                },
                env,
            )
            other = worker_decision(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(sibling / "result.json")},
                },
                env,
            )
            self.assertIsNone(own)
            self.assertEqual(other["decision"], "block")

    def test_private_temp_shell_reads_cannot_cross_assigned_job_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            private = root / "private-tmp" / "job-one"
            sibling = root / "private-tmp" / "job-two"
            repo.mkdir()
            private.mkdir(parents=True)
            sibling.mkdir()
            env = {
                "CODEX_SEATS_MANAGED_WORKER": "1",
                "CODEX_SEATS_TASK_ROOT": str(repo),
                "CODEX_SEATS_CONTROL_ROOT": str(root),
                "CODEX_SEATS_PRIVATE_TEMP": str(private),
                "CODEX_SEATS_WRITE_SCOPE": json.dumps(
                    [{"path": ".", "type": "tree"}]
                ),
                "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
                "CODEX_SEATS_PROTECTED_PATHS": "[]",
            }

            for tool_name in ("Read", "Glob", "Grep"):
                direct = worker_decision(
                    {
                        "tool_name": tool_name,
                        "tool_input": {"path": str(sibling)},
                    },
                    env,
                )
                self.assertEqual(direct["decision"], "block", tool_name)
            self.assertIsNone(
                worker_decision(
                    {
                        "tool_name": "Read",
                        "tool_input": {"file_path": str(private / "result.json")},
                    },
                    env,
                )
            )

            denied_commands = (
                (f"cat {sibling / 'secret.txt'}", str(repo)),
                ("cat ../job-two/secret.txt", str(private)),
                (f"ls {private}/../job-two", str(repo)),
                ("ls $CODEX_SEATS_PRIVATE_TEMP/..", str(repo)),
                ("target=../job-two; cat \"$target/secret.txt\"", str(private)),
                (
                    'cat "$(dirname "$CODEX_SEATS_PRIVATE_TEMP")/job-two/secret.txt"',
                    str(repo),
                ),
                ("rg needle $CODEX_SEATS_CONTROL_ROOT/private-tmp/job-two", str(repo)),
                ("find private-tmp/job-two -type f", str(root)),
                (f"rg --files {sibling}", str(repo)),
                (f"grep -f {sibling / 'pattern'} README.md", str(repo)),
                (f"wc -c < {sibling / 'secret.txt'}", str(repo)),
                ("sh -c 'cat ../job-two/secret.txt'", str(private)),
                (f"cat /../{str(sibling).lstrip('/')}/secret.txt", str(repo)),
                (f"touch {sibling / 'written.txt'}", str(repo)),
            )
            for command, cwd in denied_commands:
                decision = worker_decision(
                    {
                        "tool_name": "Bash",
                        "cwd": cwd,
                        "tool_input": {"command": command},
                    },
                    env,
                )
                self.assertEqual(decision["decision"], "block", command)

            allowed_commands = (
                f"cat {private / 'result.json'}",
                f"touch {private / 'result.json'}",
                "cat README.md",
                "cat ../README.md",
                "touch ordinary.txt",
                "rg -n 'private-tmp/job-two' README.md",
                "printf '/private-tmp/job-two is documentation'",
            )
            for command in allowed_commands:
                self.assertIsNone(
                    worker_decision(
                        {
                            "tool_name": "Bash",
                            "cwd": str(repo),
                            "tool_input": {"command": command},
                        },
                        env,
                    ),
                    command,
                )

    def test_source_text_with_a_method_named_release_is_not_an_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = {
                "CODEX_SEATS_MANAGED_WORKER": "1",
                "CODEX_SEATS_TASK_ROOT": str(root),
                "CODEX_SEATS_CONTROL_ROOT": str(root),
                "CODEX_SEATS_WRITE_SCOPE": json.dumps([{"path": ".", "type": "tree"}]),
                "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
                "CODEX_SEATS_PROTECTED_PATHS": "[]",
            }
            patch = "*** Update File: module.py\n+def release(self):\n+    return None"
            self.assertIsNone(
                worker_decision(
                    {"tool_name": "apply_patch", "tool_input": {"patch": patch}},
                    env,
                )
            )

    def test_patch_content_is_not_executed_but_actual_controls_stay_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = {
                "CODEX_SEATS_MANAGED_WORKER": "1",
                "CODEX_SEATS_TASK_ROOT": str(root),
                "CODEX_SEATS_CONTROL_ROOT": str(root),
                "CODEX_SEATS_WRITE_SCOPE": json.dumps(
                    [{"path": ".", "type": "tree"}]
                ),
                "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
                "CODEX_SEATS_PROTECTED_PATHS": "[]",
            }
            patch = (
                f"*** Begin Patch\n*** Update File: {root / 'README.md'}\n"
                "@@\n+Example: codex-seat dispatch status /outside/example.json\n"
                "+Example only: touch /outside/no and read /outside/.env\n"
                "*** End Patch"
            )
            self.assertIsNone(
                worker_decision(
                    {"tool_name": "apply_patch", "tool_input": {"patch": patch}},
                    env,
                )
            )
            dispatch_control = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": "codex-seat dispatch status"},
                },
                env,
            )
            self.assertEqual(dispatch_control["decision"], "block")
            protected_action = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": "deploy fixture"},
                },
                env,
            )
            self.assertEqual(protected_action["decision"], "block")
            read_scan = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {
                        "cmd": "rg -n 'deploy|codex-seat dispatch' README.md"
                    },
                },
                env,
            )
            self.assertIsNone(read_scan)
            compound = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": "rg deploy README.md; deploy fixture"},
                },
                env,
            )
            self.assertEqual(compound["decision"], "block")
            multiline = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": "rg deploy README.md\ndeploy fixture"},
                },
                env,
            )
            self.assertEqual(multiline["decision"], "block")
            substitution = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {
                        "cmd": 'rg "$(deploy fixture)" README.md'
                    },
                },
                env,
            )
            literal_substitution = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {
                        "cmd": "rg '$(deploy fixture)' README.md"
                    },
                },
                env,
            )
            self.assertEqual(substitution["decision"], "block")
            self.assertIsNone(literal_substitution)

    def test_git_global_options_cannot_hide_production_subcommands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env = {
                "CODEX_SEATS_MANAGED_WORKER": "1",
                "CODEX_SEATS_TASK_ROOT": str(root),
                "CODEX_SEATS_CONTROL_ROOT": str(root),
                "CODEX_SEATS_WRITE_SCOPE": json.dumps(
                    [{"path": ".", "type": "tree"}]
                ),
                "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
                "CODEX_SEATS_PROTECTED_PATHS": "[]",
            }
            denied = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {
                        "cmd": f"git -C {shlex.quote(str(root))} push origin topic"
                    },
                },
                env,
            )
            unauthorized_commit = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {
                        "cmd": f"git -C {shlex.quote(str(root))} commit -m fixture"
                    },
                },
                env,
            )
            allowed = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {
                        "cmd": f"git -C {shlex.quote(str(root))} status --short"
                    },
                },
                env,
            )
            authorized_env = {**env, "CODEX_SEATS_ALLOW_GIT_WRITE": "1"}
            attempted_authorized_commit = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {
                        "cmd": f"git -C {shlex.quote(str(root))} commit -m fixture"
                    },
                },
                authorized_env,
            )
            wrapped_push = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": "env git push origin topic"},
                },
                authorized_env,
            )
            alias_push = worker_decision(
                {
                    "tool_name": "exec_command",
                    "tool_input": {
                        "cmd": "git -c alias.ship=push ship origin topic"
                    },
                },
                authorized_env,
            )
            self.assertEqual(denied["decision"], "block")
            self.assertEqual(unauthorized_commit["decision"], "block")
            self.assertIsNone(allowed)
            self.assertEqual(attempted_authorized_commit["decision"], "block")
            self.assertEqual(wrapped_push["decision"], "block")
            self.assertEqual(alias_push["decision"], "block")


def test_git_pointer_symlink_and_target_are_both_protected(tmp_path):
    import tomllib

    repo = _git_repository(tmp_path / "repo")
    actual = tmp_path.resolve() / "admin"
    pointer = repo / ".git"
    pointer.rename(actual)
    pointer.symlink_to(actual, target_is_directory=True)
    private = tmp_path / "private"
    private.mkdir()
    hook = tmp_path / "hook"
    hook.write_text("")
    filesystem = tomllib.loads(worker_permission_profile(
        "job", repo, ["."], [], private,
    ))["permissions"]["job"]["filesystem"]
    claude = claude_job_settings(repo, ["."], [], private, hook)
    for path in (pointer, actual):
        assert filesystem[str(path)] == "read"
        assert str(path) in claude["sandbox"]["filesystem"]["denyWrite"]
        assert f"Write(/{path})" in claude["permissions"]["deny"]


if __name__ == "__main__":
    unittest.main()
