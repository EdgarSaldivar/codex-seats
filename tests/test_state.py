import tempfile
import unittest
import errno
import json
import multiprocessing
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from codex_seats.state import (
    Ledger,
    PoolSnapshotError,
    atomic_write_json,
    claim_alive,
    default_task_size,
    effective_claude_mode,
    load_settings,
    new_seat,
    read_pool_snapshot,
    refresh_transients,
    reserve_for,
    runtime_seat_home,
    default_settings,
    seat_roles,
    set_claude_mode,
    settings_lock,
    task_min_headroom,
    usable_headroom,
)
from codex_seats.policy_settings import apply_policy, policy_report, preview_policy
from codex_seats.seat_selection import pick_seat
from registry_fixture import registry_settings, write_settings


def test_corrupt_settings_never_synthesize_legacy_capacity(tmp_path):
    path = tmp_path / "settings.json"
    for content in ("{broken", "[]", json.dumps({"schema": 3, "pool": "bad"})):
        path.write_text(content)
        settings, warnings = load_settings(tmp_path)
        assert settings["schema"] == 3
        assert settings["pool"]["seats"] == {}
        assert any("repair settings.json" in warning for warning in warnings)


def test_orchestration_defaults_ignore_obsolete_probe_mode(tmp_path):
    settings = default_settings()
    assert "ui" not in settings
    assert "pool.probe_mode" not in policy_report(settings)
    assert policy_report(settings)["execution.head_worker_fallback"] is True
    settings["pool"]["probe_mode"] = "terminal"
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    loaded, warnings = load_settings(tmp_path)
    assert policy_report(loaded) == policy_report(default_settings())
    assert any("unknown setting 'pool.probe_mode'" in warning for warning in warnings)


def test_legacy_ui_section_loads_and_survives_policy_writes_without_warnings(tmp_path):
    settings = default_settings()
    legacy = {"theme": "dark", "density": "compact", "accent": "indigo"}
    settings["ui"] = legacy
    (tmp_path / "settings.json").write_text(json.dumps(settings) + "\n")
    loaded, warnings = load_settings(tmp_path)
    assert warnings == []
    assert policy_report(loaded) == policy_report(default_settings())
    with mock.patch.dict(os.environ, {"CODEX_SEATS_MANAGED_WORKER": "0"}):
        assert apply_policy(tmp_path, preview_policy(tmp_path, {"review.mode": "advisory"}))
    written = json.loads((tmp_path / "settings.json").read_text())
    assert written["review"]["mode"] == "advisory"
    assert written["ui"] == legacy
    assert load_settings(tmp_path)[1] == []


def _hold_ledger_write(root: str, entered, release) -> None:
    with Ledger(Path(root)) as data:
        data["jobs"].append({"id": "committed-after-exclusive-lock"})
        entered.set()
        release.wait(5)


class SettingsTests(unittest.TestCase):
    def test_invalid_local_registry_disables_only_local_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = registry_settings()
            settings["providers"]["claude"]["mode"] = "opportunistic"
            settings["local_hosts"] = {"bad": {"unexpected": True}}
            (root / "settings.json").write_text(json.dumps(settings))
            loaded, warnings = load_settings(root)
        self.assertEqual(loaded["pool"]["seats"], settings["pool"]["seats"])
        self.assertEqual(loaded["providers"]["claude"]["mode"], "opportunistic")
        self.assertEqual(loaded["providers"]["local"]["mode"], "off")
        self.assertEqual(loaded["local_hosts"], {})
        self.assertTrue(any("local routing forced off" in item for item in warnings))

    def test_default_settings_have_no_registered_projects(self):
        self.assertEqual(default_settings()["projects"], {})
        self.assertNotIn(
            "coordination_overhead_target_pct", default_settings()["execution"]
        )

    def test_operational_roles_cover_every_active_registered_seat(self):
        settings = registry_settings(12)
        settings["pool"]["seats"]["seat11"]["lifecycle"] = "draining"
        roles = seat_roles(settings)
        self.assertEqual(roles["seat1"], "primary")
        self.assertEqual(roles["seat2"], "worker")
        self.assertEqual(roles["seat12"], "worker")
        self.assertNotIn("seat11", roles)
        self.assertEqual(len(roles), 11)

    def test_new_defaults_are_an_empty_schema_three_registry(self):
        defaults = default_settings()
        self.assertEqual(defaults["schema"], 3)
        self.assertEqual(defaults["pool"]["seats"], {})
        self.assertIsNone(defaults["pool"]["primary"])
        self.assertNotIn("workers", defaults["pool"])
        self.assertNotIn("primary_home", defaults["pool"])

    def test_unknown_keys_warn_but_survive_and_comments_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text(json.dumps({
                "schema": 3,
                "_comment": "ignored",
                "future_top": {"enabled": True},
                "pool": {"_comment": "ignored", "future_pool": 7},
                "projects": {"local": {"path": "/repo", "future_project": 8}},
            }))
            settings, warnings = load_settings(root)
        self.assertEqual(settings["future_top"], {"enabled": True})
        self.assertEqual(settings["pool"]["future_pool"], 7)
        self.assertEqual(settings["projects"]["local"]["future_project"], 8)
        self.assertEqual(
            warnings,
            [
                "unknown setting 'future_top' was preserved; check its spelling or upgrade Codex Seats if it belongs to a newer schema",
                "unknown setting 'pool.future_pool' was preserved; check its spelling or upgrade Codex Seats if it belongs to a newer schema",
                "unknown setting 'projects.local.future_project' was preserved; check its spelling or upgrade Codex Seats if it belongs to a newer schema",
            ],
        )

    def test_recognized_compatibility_metadata_is_preserved_without_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text(json.dumps({
                "schema": 3,
                "execution": {"coordination_overhead_target_pct": 12.5},
                "projects": {
                    "local": {
                        "path": "/repo",
                        "protected_branches": ["main", "release/*"],
                    }
                },
            }))
            settings, warnings = load_settings(root)
        self.assertEqual(
            settings["execution"]["coordination_overhead_target_pct"], 12.5
        )
        self.assertEqual(
            settings["projects"]["local"]["protected_branches"],
            ["main", "release/*"],
        )
        self.assertEqual(warnings, [])

    def test_legacy_claude_role_defaults_upgrade_to_current_model_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "schema": 3,
                        "providers": {
                            "claude": {
                                "model_roles": {
                                    "frontier": "claude-fable-5",
                                    "complex": "claude-opus-4-8",
                                    "routine": "sonnet",
                                }
                            }
                        },
                    }
                )
            )
            settings, warnings = load_settings(root)
        self.assertEqual(
            settings["providers"]["claude"]["model_roles"],
            {
                "frontier": "claude-fable-5-1",
                "complex": "claude-opus-5-5",
                "routine": "claude-sonnet-5",
            },
        )
        self.assertEqual(len(warnings), 3)
        self.assertTrue(all("upgraded" in warning for warning in warnings))

    def test_previous_codex_complex_default_upgrades_without_rewriting_other_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "schema": 3,
                        "providers": {
                            "codex": {
                                "model_roles": {
                                    "frontier": "gpt-6-astra",
                                    "complex": "gpt-5.6-sol",
                                    "routine": "gpt-5.6-sol",
                                }
                            }
                        },
                    }
                )
            )
            settings, warnings = load_settings(root)
        self.assertEqual(
            settings["providers"]["codex"]["model_roles"],
            {
                "frontier": "gpt-6-astra",
                "complex": "gpt-6-sol",
                "routine": "gpt-5.6-sol",
            },
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("providers.codex.model_roles.complex upgraded", warnings[0])

    def test_top_level_coordination_overhead_metadata_remains_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text(json.dumps({
                "schema": 3,
                "coordination_overhead_target_pct": 12,
            }))
            settings, warnings = load_settings(root)
        self.assertEqual(settings["coordination_overhead_target_pct"], 12)
        self.assertTrue(
            any(
                "unknown setting 'coordination_overhead_target_pct'" in warning
                for warning in warnings
            )
        )

    def test_invalid_compatibility_metadata_fails_settings_shape_safely(self):
        def with_protected_branches(value):
            return {"projects": {"local": {
                "path": "/repo", "protected_branches": value,
            }}}

        invalid_values = (
            ({"execution": {"coordination_overhead_target_pct": True}}, "percentage"),
            ({"execution": {"coordination_overhead_target_pct": -1}}, "percentage"),
            ({"execution": {"coordination_overhead_target_pct": 101}}, "percentage"),
            ({"execution": {"coordination_overhead_target_pct": "12"}}, "percentage"),
            (with_protected_branches(True), "array"),
            (with_protected_branches("main"), "array"),
            (with_protected_branches([""]), "non-empty"),
            (with_protected_branches([" main"]), "whitespace"),
            (with_protected_branches(["main\n"]), "whitespace"),
            (with_protected_branches(["main", "main"]), "duplicate"),
            (with_protected_branches(["main\u007fbranch"]), "control"),
            (with_protected_branches(["x" * 257]), "at most 256"),
            (
                with_protected_branches([str(index) for index in range(65)]),
                "at most 64",
            ),
        )
        for metadata, expected in invalid_values:
            with (
                self.subTest(expected=expected),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                raw = {"schema": 3, **metadata}
                (root / "settings.json").write_text(json.dumps(raw))
                settings, warnings = load_settings(root)
            self.assertEqual(settings, default_settings())
            self.assertTrue(
                any(
                    "invalid settings shape" in warning and expected in warning
                    for warning in warnings
                ),
                warnings,
            )

    def test_settings_mutation_rejects_symlinks_and_multiply_linked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text(json.dumps(default_settings()))
            path = root / "settings.json"
            path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "non-symbolic-link"):
                set_claude_mode(root, "off")

            path.unlink()
            os.link(target, path)
            with self.assertRaisesRegex(ValueError, "singly linked"):
                set_claude_mode(root, "off")

    def test_settings_mutation_rereads_after_acquiring_shared_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            path.write_text('{"schema":3,"future":"stale"}')
            attempted = threading.Event()
            outcome = []

            def mutate() -> None:
                attempted.set()
                outcome.append(set_claude_mode(root, "manual_only"))

            with settings_lock(root):
                thread = threading.Thread(target=mutate)
                thread.start()
                self.assertTrue(attempted.wait(1))
                thread.join(0.05)
                self.assertTrue(thread.is_alive(), "mutation must wait for settings lock")
                current = '{"schema":3,"future":"latest"}'
                path.write_text(current)
            thread.join(2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(json.loads(path.read_text())["future"], "latest")
            self.assertEqual(outcome[0]["future"], "latest")
            backups = list(root.glob("settings.json.backup-*"))
            self.assertEqual([backup.read_text() for backup in backups], [current])

    def test_future_schema_and_conflicting_roles_fail_closed(self):
        conflicting = registry_settings(2)
        conflicting["pool"]["primary"] = "seat9"
        unknown_reserve = registry_settings(2)
        unknown_reserve["pool"]["reserves_pct"] = {"seat9": 5}
        for raw, expected in (
            ({"schema": 4}, "unsupported settings schema 4"),
            (conflicting, "pool.primary must name an active registered seat"),
            (unknown_reserve, "pool.reserves_pct has unknown seat id 'seat9'"),
            ({"schema": 3, "pool": {"small_task_min_headroom_pct": True}},
             "pool.small_task_min_headroom_pct must be a percentage"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "settings.json").write_text(json.dumps(raw))
                settings, warnings = load_settings(root)
            self.assertEqual(settings, default_settings())
            self.assertTrue(any(expected in warning for warning in warnings), warnings)

    def test_missing_settings_hard_disables_claude(self):
        with tempfile.TemporaryDirectory() as directory:
            settings, warnings = load_settings(Path(directory))
        self.assertEqual(settings["schema"], 3)
        self.assertEqual(settings["pool"]["seats"], {})
        self.assertIsNone(settings["pool"]["primary"])
        self.assertEqual(effective_claude_mode(settings)[0], "off")
        self.assertTrue(any("explicit seat onboarding" in warning for warning in warnings))

    def test_expired_claude_configuration_is_effectively_off(self):
        settings = default_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        settings["providers"]["claude"]["expires_at"] = "2020-01-01T00:00:00Z"
        mode, reason = effective_claude_mode(settings)
        self.assertEqual(mode, "off")
        self.assertIn("expired", reason)

    def test_invalid_settings_shape_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text('{"providers": "broken"}')
            settings, warnings = load_settings(root)
        self.assertEqual(effective_claude_mode(settings)[0], "off")
        self.assertTrue(any("shape" in warning for warning in warnings))

    def test_codex_supports_ultra_defaults_false_and_must_be_bool(self):
        self.assertIs(default_settings()["providers"]["codex"]["supports_ultra"], False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text(
                '{"providers": {"codex": {"supports_ultra": "yes"}}}'
            )
            settings, warnings = load_settings(root)
        self.assertEqual(effective_claude_mode(settings)[0], "off")
        self.assertTrue(any("shape" in warning for warning in warnings))

    def test_mode_change_refuses_to_overwrite_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            path.write_text("{broken")
            with self.assertRaises(ValueError):
                set_claude_mode(root, "off")
            self.assertEqual(path.read_text(), "{broken")

    def test_mode_change_persists_canonical_schema_three_without_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            write_settings(root, registry_settings(2))

            saved = set_claude_mode(root, "manual_only")
            on_disk = json.loads(path.read_text())

        self.assertEqual(saved, on_disk)
        self.assertEqual(on_disk["schema"], 3)
        self.assertEqual(sorted(on_disk["pool"]["seats"]), ["seat1", "seat2"])
        self.assertEqual(on_disk["providers"]["claude"]["mode"], "manual_only")

    def test_claude_cooldown_survives_off_on_toggle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = default_settings()
            settings["providers"]["claude"]["mode"] = "opportunistic"
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                provider["status"] = "cooldown"
                provider["status_until"] = datetime.now().timestamp() + 3600
                off = default_settings()
                refresh_transients(data, root, off)
                self.assertEqual(provider["status"], "off")
                refresh_transients(data, root, settings)
                self.assertEqual(provider["status"], "cooldown")

    def test_capped_seat_survives_auth_file_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            auth = root / "seat1" / "auth.json"
            auth.parent.mkdir()
            auth.write_text("{}")
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                seat["status"] = "capped"
                seat["status_until"] = datetime.now().timestamp() + 3600
                auth.unlink()
                refresh_transients(data, root, settings)
                self.assertEqual(seat["status"], "needs-login")
                auth.write_text("{}")
                refresh_transients(data, root, settings)
                self.assertEqual(seat["status"], "capped")

    def test_authoritative_auth_quarantine_survives_auth_file_status_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            auth = root / "seat1" / "auth.json"
            auth.parent.mkdir()
            auth.write_text("{}")
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                seat["status"] = "needs-login"
                seat["auth_quarantined_at"] = "2026-07-12T00:00:00Z"
                seat["auth_failure"] = "refresh token revoked"
                refresh_transients(data, root, settings)
                self.assertEqual(seat["status"], "needs-login")
                self.assertEqual(seat["auth_failure"], "refresh token revoked")

    def test_corrupt_ledger_does_not_wedge_future_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            (root / "seats.json").write_text("not json")
            with self.assertRaises(ValueError):
                with Ledger(root):
                    pass
            atomic_write_json(root / "seats.json", {"seats": {}, "jobs": []})
            with Ledger(root) as data:
                self.assertIn("seat1", data["seats"])

    def test_primary_can_share_interactive_codex_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = registry_settings(primary_home=root / "interactive")
            self.assertEqual(runtime_seat_home(root, "seat1", settings), root / "interactive")
            self.assertEqual(runtime_seat_home(root, "seat2", settings), root / "seat2")

    def test_failed_ledger_write_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = Ledger(root)
            with self.assertRaises(OSError):
                with mock.patch("codex_seats.state.atomic_write_json", side_effect=OSError("full")):
                    with ledger as data:
                        data["jobs"].append({"id": "test"})
            self.assertTrue(ledger._fh.closed)
            with Ledger(root) as data:
                self.assertEqual(data["jobs"], [])

    def test_read_only_snapshot_never_changes_files_or_mtimes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            with Ledger(root) as data:
                data["seats"]["seat1"]["pct_left"] = 73
            paths = (root, root / ".ledger.lock", root / "seats.json")
            before = {
                path: (
                    path.stat().st_mtime_ns,
                    path.stat().st_size,
                    path.read_bytes() if path.is_file() else None,
                )
                for path in paths
            }

            snapshot = read_pool_snapshot(root)
            snapshot["seats"]["seat1"]["pct_left"] = 0

            after = {
                path: (
                    path.stat().st_mtime_ns,
                    path.stat().st_size,
                    path.read_bytes() if path.is_file() else None,
                )
                for path in paths
            }
            self.assertEqual(snapshot["schema"], 6)
            self.assertEqual(before, after)

    def test_historical_queue_keys_survive_migration_as_inert_unknown_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            historical = {
                "pending_queue": [{"prompt": "never execute", "nested": [1, "two"]}],
                "disabled_queue": [{"prompt": "never move", "reason": None}],
            }
            atomic_write_json(
                root / "seats.json",
                {"schema": 5, "seats": {}, "jobs": [], **historical},
            )

            with Ledger(root) as data:
                self.assertEqual(data["pending_queue"], historical["pending_queue"])
                self.assertEqual(data["disabled_queue"], historical["disabled_queue"])
                data["jobs"].append({"id": "ordinary-write"})

            persisted = json.loads((root / "seats.json").read_text())
            self.assertEqual(persisted["schema"], 6)
            self.assertEqual(persisted["pending_queue"], historical["pending_queue"])
            self.assertEqual(persisted["disabled_queue"], historical["disabled_queue"])

    def test_new_ledger_and_snapshot_do_not_create_queue_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Ledger(root) as data:
                data["jobs"].append({"id": "persist"})
                self.assertNotIn("pending_queue", data)
                self.assertNotIn("disabled_queue", data)
            snapshot = read_pool_snapshot(root)
            self.assertNotIn("pending_queue", snapshot)
            self.assertNotIn("disabled_queue", snapshot)

    def test_read_only_snapshot_sanitizes_sparse_data_without_persisting_repairs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            lock = root / ".ledger.lock"
            state = root / "seats.json"
            lock.write_bytes(b"")
            state.write_text(json.dumps({"seats": {"seat1": {"pct_left": 42}}}))
            before = (state.read_bytes(), state.stat().st_mtime_ns, lock.stat().st_mtime_ns)

            snapshot = read_pool_snapshot(root)

            self.assertEqual(snapshot["seats"]["seat1"]["pct_left"], 42)
            self.assertIn("account_window_observation", snapshot["seats"]["seat1"])
            self.assertIn("seat5", snapshot["seats"])
            self.assertIn("claude", snapshot["providers"])
            self.assertEqual(
                before,
                (state.read_bytes(), state.stat().st_mtime_ns, lock.stat().st_mtime_ns),
            )

    def test_read_only_snapshot_does_not_create_a_missing_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "seats.json"
            state.write_text(json.dumps({"seats": {}, "jobs": []}))

            with self.assertRaisesRegex(PoolSnapshotError, "lock file is missing"):
                read_pool_snapshot(root)

            self.assertFalse((root / ".ledger.lock").exists())
            self.assertEqual({path.name for path in root.iterdir()}, {"seats.json"})

    def test_read_only_snapshot_waits_for_concurrent_writer_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Ledger(root):
                pass
            context = multiprocessing.get_context("fork")
            entered = context.Event()
            release = context.Event()
            writer = context.Process(
                target=_hold_ledger_write,
                args=(str(root), entered, release),
            )
            writer.start()
            self.assertTrue(entered.wait(3))
            result: list[dict] = []
            failure: list[BaseException] = []

            def read_snapshot() -> None:
                try:
                    result.append(read_pool_snapshot(root))
                except BaseException as exc:
                    failure.append(exc)

            reader = threading.Thread(target=read_snapshot)
            reader.start()
            time.sleep(0.1)
            self.assertTrue(reader.is_alive(), "shared reader bypassed the writer's exclusive lock")
            release.set()
            reader.join(3)
            writer.join(3)

            self.assertFalse(reader.is_alive())
            self.assertEqual(writer.exitcode, 0)
            self.assertEqual(failure, [])
            self.assertEqual(
                result[0]["jobs"][-1]["id"],
                "committed-after-exclusive-lock",
            )

    def test_live_child_keeps_claim_after_dispatcher_exits(self):
        claim = {"dispatcher_pid": 101, "child_pid": 202}
        with mock.patch("codex_seats.state.pid_alive", side_effect=lambda pid: pid == 202):
            self.assertTrue(claim_alive(claim))
        with mock.patch("codex_seats.state.pid_alive", return_value=False):
            self.assertFalse(claim_alive(claim))

    def test_permission_denied_pid_is_unknown_but_treated_as_alive(self):
        with mock.patch("codex_seats.state.os.kill", side_effect=PermissionError(errno.EPERM, "denied")):
            from codex_seats.state import pid_alive

            self.assertTrue(pid_alive(123))

    def test_only_missing_pid_is_treated_as_dead(self):
        from codex_seats.state import pid_alive

        with mock.patch("codex_seats.state.os.kill", side_effect=OSError(errno.ESRCH, "missing")):
            self.assertFalse(pid_alive(123))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.settings = registry_settings()
        self.seats = {seat_id: new_seat() for seat_id in self.settings["pool"]["seats"]}
        for seat in self.seats.values():
            timestamp = datetime.now().astimezone().isoformat()
            seat.update(
                {
                    "status": "ok",
                    "pct_left": 100,
                    "probed_at": timestamp,
                    "usage_updated_at": timestamp,
                    "last_probe_limits": {"monthly": {"pct": 100}},
                }
            )
        self.seats["seat5"]["usage_updated_at"] = datetime.now().astimezone().isoformat()
        self.seats["seat5"]["last_probe_limits"] = {
            "5h": {
                "pct": 100,
                "resets_at": time.time() + 3600,
                "window_minutes": 300,
            },
            "weekly": {"pct": 100, "window_minutes": 10080},
        }

    def test_every_seat_is_admitted_independent_of_operational_role(self):
        for expected in self.seats:
            seats = json.loads(json.dumps(self.seats))
            for seat_id in self.seats:
                if seat_id != expected:
                    seats[seat_id]["status"] = "capped"
            self.assertEqual(pick_seat(seats, self.settings, "small"), expected)

    def test_worker_uses_primary_when_no_secondary_can_eventually_fit(self):
        for seat_id in ("seat2", "seat3", "seat4", "seat5"):
            self.seats[seat_id]["status"] = "capped"
        self.seats["seat1"]["pct_left"] = 80
        self.assertEqual(pick_seat(self.seats, self.settings, "small"), "seat1")

    def test_primary_legacy_reserve_does_not_block_last_capacity(self):
        for seat_id in ["seat2", "seat3", "seat4", "seat5"]:
            self.seats[seat_id]["status"] = "capped"
        self.seats["seat1"]["pct_left"] = 20
        self.assertEqual(pick_seat(self.seats, self.settings, "small"), "seat1")

    def test_small_task_skips_seat_below_safety_floor(self):
        self.assertEqual(task_min_headroom(self.settings, "small"), 5)
        for seat_id in ("seat1", "seat2", "seat3", "seat4"):
            self.seats[seat_id]["status"] = "capped"
        self.seats["seat5"]["pct_left"] = 4
        self.seats["seat5"]["last_probe_limits"] = {"monthly": {"pct": 4}}
        self.assertIsNone(pick_seat(self.seats, self.settings, "small"))
        self.seats["seat5"]["pct_left"] = 40
        self.seats["seat5"]["last_probe_limits"] = {"monthly": {"pct": 40}}
        self.assertEqual(pick_seat(self.seats, self.settings, "small"), "seat5")

    def test_automatic_selection_uses_per_seat_floor_override(self):
        self.settings["pool"]["task_min_headroom_pct_by_seat"] = {
            "seat5": {"small": 95, "big": 95}
        }
        for seat_id in ("seat1", "seat2", "seat3", "seat4"):
            self.seats[seat_id]["status"] = "capped"
        self.seats["seat5"]["pct_left"] = 90
        self.assertIsNone(pick_seat(self.seats, self.settings, "small"))
        self.settings["pool"]["task_min_headroom_pct_by_seat"]["seat5"]["small"] = 85
        self.assertEqual(pick_seat(self.seats, self.settings, "small"), "seat5")

    def test_default_size_is_conservative_by_kind(self):
        self.assertEqual(default_task_size(self.settings, "implementation"), "big")
        self.assertEqual(default_task_size(self.settings, "mechanical"), "small")

    def test_legacy_fractional_reserve_is_inert(self):
        self.seats["seat1"]["pct_left"] = 50
        self.seats["seat1"]["last_probe_limits"] = {}
        for reserve in (0.5, 6.5):
            with self.subTest(reserve=reserve):
                self.settings["pool"]["reserves_pct"]["seat1"] = reserve
                self.assertEqual(reserve_for(self.settings, "seat1", self.seats["seat1"]), 0)
                self.assertEqual(usable_headroom(self.settings, "seat1", self.seats["seat1"]), 50)

    def test_legacy_reserve_is_inert_near_reset(self):
        reference = datetime.now().astimezone().replace(second=0, microsecond=0)
        reset = reference + timedelta(hours=1)
        self.seats["seat1"]["last_probe_limits"] = {
            "monthly": {"resets": reset.strftime("resets %H:%M on %-d %b")}
        }
        self.assertEqual(reserve_for(self.settings, "seat1", self.seats["seat1"], reference), 0)

    def test_legacy_reserve_is_inert_with_long_horizon_window(self):
        reference = datetime.now().astimezone().replace(second=0, microsecond=0)
        monthly = reference + timedelta(days=10)
        short = reference + timedelta(hours=1)
        self.seats["seat1"]["last_probe_limits"] = {
            "monthly": {"resets": monthly.strftime("resets %H:%M on %-d %b")},
            "5h": {"resets": short.strftime("resets %H:%M")},
        }
        self.assertEqual(reserve_for(self.settings, "seat1", self.seats["seat1"], reference), 0)

    def test_legacy_reserve_is_inert_with_unparseable_monthly_reset(self):
        reference = datetime.now().astimezone().replace(second=0, microsecond=0)
        weekly = reference + timedelta(hours=1)
        self.seats["seat1"]["last_probe_limits"] = {
            "monthly": {"resets": "garbled"},
            "weekly": {"resets": weekly.strftime("resets %H:%M on %-d %b")},
        }
        self.assertEqual(reserve_for(self.settings, "seat1", self.seats["seat1"], reference), 0)

    def test_legacy_reserve_is_inert_regardless_of_reported_duration(self):
        reference = datetime.now().astimezone().replace(second=0, microsecond=0)
        self.seats["seat1"]["last_probe_limits"] = {
            "quick": {
                "window_minutes": 300,
                "resets_at": (reference + timedelta(days=2)).timestamp(),
            },
            "account-period": {
                "window_minutes": 10080,
                "resets_at": (reference + timedelta(hours=1)).timestamp(),
            },
        }
        self.assertEqual(
            reserve_for(self.settings, "seat1", self.seats["seat1"], reference), 0
        )


if __name__ == "__main__":
    unittest.main()
