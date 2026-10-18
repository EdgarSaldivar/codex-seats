import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_seats import dispatch_protocol as dp
from codex_seats.display import DashboardViewState, dashboard, role_badges, segment
from codex_seats.display_status import status_json
from codex_seats.state import (
    Ledger,
    iso,
    load_claude_rate_limits,
    default_settings,
)
from registry_fixture import registry_settings, write_settings


class DisplayTests(unittest.TestCase):
    ansi = re.compile(r"\x1b\[[0-9;]*m")

    @staticmethod
    def display_width(text: str) -> int:
        return sum(
            0
            if unicodedata.combining(character)
            else 2
            if unicodedata.east_asian_width(character) in {"W", "F"}
            else 1
            for character in text
        )

    @staticmethod
    def healthy_pool(root: Path, settings: dict, current: float) -> None:
        seat_ids = list(settings["pool"]["seats"])
        write_settings(root, settings)
        for seat_id in seat_ids:
            home = root / seat_id
            home.mkdir(exist_ok=True)
            (home / "auth.json").write_text("{}")
        with Ledger(root) as data:
            for index, seat_id in enumerate(seat_ids):
                seat = data["seats"][seat_id]
                seat.update(
                    {
                        "status": "ok",
                        "type": "window",
                        "pct_left": 90 - index * 5,
                        "probed_at": iso(current - 20),
                        "usage_updated_at": iso(current - 20),
                        "usage_source": "account",
                        "last_probe_limits": {
                            "weekly": {
                                "pct": 90 - index * 5,
                                "resets_at": current + 7 * 86400,
                                "window_minutes": 10080,
                            }
                        },
                        "active_window_observation": {
                            "source": "account",
                            "complete": True,
                            "observed_at": current - 20,
                            "observed_windows": ["weekly"],
                            "active_windows": ["weekly"],
                        },
                    }
                )
        settings["providers"]["claude"]["mode"] = "opportunistic"
        (root / "claude-usage.json").write_text(
            json.dumps(
                {
                    "updated_at": current - 10,
                    "five_hour": {
                        "used_percentage": 25,
                        "resets_at": current + 3600,
                    },
                    "seven_day": {
                        "used_percentage": 40,
                        "resets_at": current + 7 * 86400,
                    },
                }
            )
        )

    def test_claude_subscription_tier_is_visible_in_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = 1_784_250_000.0
            settings = write_settings(root, registry_settings())
            settings["providers"]["claude"]["subscription_tier"] = "Max 5x"
            self.healthy_pool(root, settings, current)
            wide = dashboard(root, settings, [], color=False, width=160, at=current)
            compact = dashboard(root, settings, [], color=False, width=80, at=current)
            self.assertIn("C claude Max 5x", wide)
            self.assertIn("C claude Max 5x", compact)
            settings["providers"]["claude"]["subscription_tier"] = "Team Premium"
            self.assertIn(
                "claude Team Premium",
                dashboard(root, settings, [], color=False, width=120, at=current),
            )
            narrow = dashboard(root, settings, [], color=False, width=50, at=current)
            self.assertIn("C claude Team Premium", narrow)
            self.assertIn("7d ███░░  60%", narrow)

    def test_codex_dashboard_uses_configured_plan_name_beside_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = 1_784_250_000.0
            settings = default_settings()
            seat = {
                "label": "Primary",
                "lifecycle": "active",
                "home": None,
                "capacity_profile": "business-premium",
                "relative_capacity": 5.0,
                "capacity_source": "openai-published-baseline",
                "capacity_verified_at": "2026-09-03T00:00:00Z",
            }
            settings["pool"]["seats"] = {"seat1": seat}
            settings["pool"]["primary"] = "seat1"
            self.healthy_pool(root, settings, current)
            wide = dashboard(root, settings, [], color=False, width=160, at=current)
            compact = dashboard(root, settings, [], color=False, width=80, at=current)
            self.assertIn("P Primary Premium 5x", wide)
            self.assertIn("P Primary Premium", compact)
            self.assertNotIn("Primary@5x", wide)

            seat["capacity_profile"] = "business-standard"
            self.assertIn(
                "Primary Standard 1x",
                dashboard(root, settings, [], color=False, width=160, at=current),
            )

            seat["capacity_profile"] = "custom-profile"
            self.assertIn(
                "Primary Custom profile",
                dashboard(root, settings, [], color=False, width=160, at=current),
            )

    def test_inactive_seats_leave_operational_views_but_draining_remains(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = default_settings()
            base = {
                "home": None,
                "capacity_profile": "unverified",
                "relative_capacity": None,
                "capacity_source": "unverified",
                "capacity_verified_at": None,
            }
            settings["pool"]["seats"] = {
                "seat1": {**base, "label": "Active-visible", "lifecycle": "active"},
                "seat2": {
                    **base,
                    "label": "Draining-visible",
                    "lifecycle": "draining",
                },
                "seat3": {**base, "label": "Inactive-hidden", "lifecycle": "inactive"},
            }
            settings["pool"]["primary"] = "seat1"
            write_settings(root, settings)
            with Ledger(root):
                pass

            rendered = dashboard(root, settings, [], color=False, width=120)
            tmux = segment(root, settings, plain=True)
            machine = json.loads(status_json(root, settings))

            self.assertIn("Active-visible", rendered)
            self.assertIn("Draining-visible", rendered)
            self.assertNotIn("Inactive-hidden", rendered)
            self.assertNotIn("seat3", tmux)
            self.assertIn("seat3", machine["seats"])
            self.assertEqual(machine["seats"]["seat3"]["lifecycle"], "inactive")
            self.assertEqual(machine["seat_counts"]["inactive"], 1)

    def test_passive_claude_cache_and_activity_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            settings["providers"]["claude"]["mode"] = "opportunistic"
            for seat_id in settings["pool"]["seats"]:
                home = root / seat_id
                home.mkdir()
                (home / "auth.json").write_text("{}")
            with Ledger(root) as data:
                for seat in data["seats"].values():
                    seat.update(
                        {
                            "status": "ok",
                            "type": "window",
                            "pct_left": 100,
                            "probed_at": "2026-07-11T00:00:00Z",
                            "usage_updated_at": iso(time.time()),
                            "last_probe_limits": {
                                "weekly": {
                                    "pct": 100,
                                    "window_minutes": 10080,
                                    "resets_at": time.time() + 7 * 86400,
                                }
                            },
                            "active_window_observation": {
                                "source": "account",
                                "complete": True,
                                "observed_at": time.time(),
                                "observed_windows": ["weekly"],
                                "active_windows": ["weekly"],
                            },
                        }
                    )
                data["seats"]["seat5"]["in_flight"] = {"dispatcher_pid": os.getpid()}

            script = Path(__file__).parents[1] / "claude-statusline.py"
            status_input = {
                "model": {"display_name": "Opus"},
                "context_window": {"used_percentage": 12},
                "rate_limits": {
                    "five_hour": {"used_percentage": 25, "resets_at": time.time() + 3600},
                    "seven_day": {"used_percentage": 50, "resets_at": time.time() + 86400},
                },
            }
            result = subprocess.run(
                [sys.executable, str(script)],
                input=json.dumps(status_input),
                text=True,
                capture_output=True,
                check=True,
                env=dict(os.environ, CODEX_SEATS_ROOT=str(root)),
            )
            self.assertIn("5h 75% left", result.stdout)
            self.assertEqual(load_claude_rate_limits(root)["seven_day"]["remaining_percentage"], 50)

            status_segment = segment(root, settings, plain=True)
            self.assertIn("seat1@1x:100○", status_segment)
            self.assertIn("seat5@1x:100●", status_segment)
            self.assertIn("C:75/50○", status_segment)
            full_dashboard = dashboard(root, settings, [], color=False)
            busy_line = next(
                line for line in full_dashboard.splitlines() if "seat5" in line
            )
            self.assertIn("small+big", busy_line)
            self.assertIn("●", busy_line)
            self.assertIn("C claude", full_dashboard)
            self.assertIn("50%", full_dashboard)
            self.assertIn("ONGOING WORK", full_dashboard)
            machine = json.loads(status_json(root, settings))
            self.assertTrue(machine["seats"]["seat5"]["fits_big"])
            self.assertTrue(machine["claude"]["fits_small"])
            self.assertTrue(machine["claude"]["fits_big"])
            with Ledger(root) as data:
                data["providers"]["claude"]["status"] = "needs-auth"
            self.assertIn("C:ERR×", segment(root, settings, plain=True))

    def test_status_renderers_do_not_write_or_change_mtimes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = time.time()
            self.healthy_pool(root, settings, current)

            def filesystem_state():
                return {
                    str(path.relative_to(root)): (
                        path.is_dir(),
                        path.stat().st_mtime_ns,
                        path.stat().st_size,
                        path.read_bytes() if path.is_file() else None,
                    )
                    for path in (root, *root.rglob("*"))
                }

            before = filesystem_state()
            machine = json.loads(status_json(root, settings))
            rendered = dashboard(root, settings, [], color=False, at=current)
            executable = Path(__file__).parents[1] / "codex-seat"
            env = dict(
                os.environ,
                CODEX_SEATS_ROOT=str(root),
                PYTHONDONTWRITEBYTECODE="1",
            )
            json_status = subprocess.run(
                [sys.executable, str(executable), "status", "--json"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            normal_status = subprocess.run(
                [sys.executable, str(executable), "status"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            after = filesystem_state()

            self.assertIn("seats", machine)
            self.assertIn("codex-seats", rendered)
            self.assertIn('"seats"', json_status.stdout)
            self.assertIn("codex-seats", normal_status.stdout)
            self.assertEqual(before, after)

    def test_status_pending_counts_only_durable_ready_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            with Ledger(root) as data:
                data["pending_queue"] = [{"prompt": "historical"}] * 9
            request_id = dp.new_request_id()
            dp.write_request(root, {"request_id": request_id})
            inbox = dp.queue_paths(root)["inbox"]
            (inbox / "not-a-request.ready").write_text("{}")
            (inbox / f"{dp.new_request_id()}.ready").symlink_to(
                inbox / f"{request_id}.ready"
            )

            machine = json.loads(status_json(root, settings))

            self.assertEqual(machine["pending"], 1)

    def test_status_fails_clearly_without_creating_a_missing_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "seats.json").write_text(
                json.dumps({"seats": {}, "jobs": []})
            )
            executable = Path(__file__).parents[1] / "codex-seat"
            result = subprocess.run(
                [sys.executable, str(executable), "status", "--json"],
                env=dict(
                    os.environ,
                    CODEX_SEATS_ROOT=str(root),
                    PYTHONDONTWRITEBYTECODE="1",
                ),
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("lock file is missing", result.stderr)
            self.assertFalse((root / ".ledger.lock").exists())

    def test_segment_reports_live_mode_effort_without_legacy_reserve_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            for seat_id in settings["pool"]["seats"]:
                home = root / seat_id
                home.mkdir()
                (home / "auth.json").write_text("{}")
            with Ledger(root) as data:
                for seat in data["seats"].values():
                    seat.update({"status": "ok", "pct_left": 95})
                data["seats"]["seat2"].update({"pct_left": 20})
                data["seats"]["seat2"]["in_flight"] = {
                    "dispatcher_pid": os.getpid(),
                    "kind": "control",
                    "starting_pct": 90,
                    "session": {
                        "mode": "plan",
                        "effort": "ultra",
                        "approvals_reviewer": "auto_review",
                    },
                }
            with mock.patch.dict(os.environ, {"CODEX_SEATS_NO_NOTIFY": "1"}):
                rendered = segment(root, settings, plain=True)
            self.assertIn("MODE:PLAN", rendered)
            self.assertIn("E:ULTRA", rendered)
            self.assertNotIn("ROTATE:seat2", rendered)

    def test_active_job_row_shows_seat_kind_and_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            for seat_id in settings["pool"]["seats"]:
                (root / seat_id).mkdir()
            with Ledger(root) as data:
                data["seats"]["seat3"]["in_flight"] = {
                    "dispatcher_pid": os.getpid(),
                    "kind": "planning",
                    "label": "map the migration",
                }
            rendered = dashboard(root, settings, [], color=False)
            self.assertIn("ONGOING WORK", rendered)
            self.assertIn("● map the migration · Running · seat3 · planning", rendered)

    def test_background_work_rows_counts_and_ledger_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            request_id = "a" * 32
            with Ledger(root) as data:
                data["seats"]["seat3"]["in_flight"] = {
                    "dispatcher_pid": os.getpid(),
                    "kind": "review",
                    "size": "small",
                    "label": request_id,
                    "started_at": iso(time.time() - 9),
                }
            jobs = [
                {
                    "request_id": request_id,
                    "state": "running",
                    "requested_provider": "auto",
                    "provider": "codex",
                    "seat": "seat3",
                    "kind": "review",
                    "size": "small",
                    "elapsed_seconds": 9,
                    "label": "review bounded patch",
                },
                {
                    "request_id": "b" * 32,
                    "state": "queued",
                    "requested_provider": "claude",
                    "provider": None,
                    "seat": None,
                    "kind": "planning",
                    "size": "big",
                    "elapsed_seconds": 4,
                    "label": "plan next step",
                },
                {
                    "request_id": "c" * 32,
                    "state": "waiting_capacity",
                    "requested_provider": "auto",
                    "provider": None,
                    "seat": None,
                    "kind": "implementation",
                    "size": "big",
                    "elapsed_seconds": 12,
                    "label": "wait for capacity",
                },
            ]
            with mock.patch(
                "codex_seats.display.dispatch_client.live_jobs", return_value=jobs
            ):
                rendered = dashboard(root, settings, [], color=False, width=140)

            self.assertIn("ONGOING WORK", rendered)
            self.assertIn("Running 1 · Queued 1 · Waiting 1", rendered)
            self.assertIn("● review bounded patch · Running · codex/seat3 · review/small", rendered)
            self.assertIn("○ plan next step · Queued · claude · planning/big", rendered)
            self.assertIn("◐ wait for capacity · Waiting for capacity · auto", rendered)
            self.assertLess(
                rendered.index("review bounded patch"),
                rendered.index("plan next step"),
            )
            self.assertLess(rendered.index("plan next step"), rendered.index("wait for capacity"))
            self.assertEqual(rendered.count("aaaaaaaa"), 1)
            self.assertNotIn("Managed review · Running · W1", rendered)

    def test_segment_keeps_background_marker_while_head_is_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            with Ledger(root) as data:
                data["jobs"] = []
            jobs = [
                {"request_id": "a" * 32, "state": "queued"},
                {"request_id": "b" * 32, "state": "running"},
            ]
            with mock.patch(
                "codex_seats.display.dispatch_client.live_jobs", return_value=jobs
            ):
                rendered = segment(root, settings, plain=True)
            self.assertIn("BG:1q/1r/0c", rendered)

    def test_status_json_adds_backward_compatible_background_object(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            with Ledger(root) as data:
                data["jobs"].append({"status": "completed"})
            jobs = [{"request_id": "a" * 32, "state": "waiting_capacity"}]
            with mock.patch(
                "codex_seats.display_status.dispatch_client.live_jobs",
                return_value=jobs,
            ):
                machine = json.loads(status_json(root, settings))
            self.assertIn("seats", machine)
            self.assertEqual(machine["background_jobs"]["jobs"], jobs)
            self.assertEqual(
                machine["background_jobs"]["counts"],
                {
                    "queued": 0,
                    "running": 0,
                    "capacity": 1,
                    "recovering": 0,
                    "expired": 0,
                },
            )
            self.assertIn(
                machine["background_jobs"]["wait_backend"],
                {"kqueue", "inotify", "poll"},
            )
            self.assertIsNone(machine["background_jobs"]["error"])

    def test_status_json_reports_background_queue_security_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            with Ledger(root) as data:
                data["jobs"].append({"status": "completed"})
            paths = dp.ensure_queue(root)
            paths["processing"].chmod(0o750)

            machine = json.loads(status_json(root, settings))

            self.assertEqual(machine["background_jobs"]["jobs"], [])
            self.assertEqual(
                machine["background_jobs"]["error"],
                "queue security validation failed",
            )

    def test_signal_table_normal_and_compact_are_width_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = 1_784_250_000.0
            self.healthy_pool(root, settings, current)
            refresh = SimpleNamespace(
                refreshing=False,
                last_result="Codex 5/5; Claude ok",
                last_success_at=current - 30,
                last_finished_at=current - 30,
                next_due=1090.0,
                completion_generation=3,
            )
            normal = dashboard(
                root,
                settings,
                [],
                color=False,
                width=120,
                refresh_snapshot=refresh,
                at=current,
                monotonic_at=1000,
            )
            compact = dashboard(
                root,
                settings,
                [],
                color=False,
                width=80,
                refresh_snapshot=refresh,
                at=current,
                monotonic_at=1000,
            )

            self.assertTrue(all(len(line) <= 120 for line in normal.splitlines()))
            self.assertTrue(all(len(line) <= 80 for line in compact.splitlines()))
            for frame in (normal, compact):
                self.assertRegex(frame, r"SEAT\s+5H LEFT\s+7D LEFT\s+RESET\s+STATUS\s+FIT\s+AGE")
            self.assertIn("updated 30s ago", normal)
            self.assertIn("next 2m", normal)
            self.assertIn("Ready 6/6", normal)
            self.assertEqual(len(re.findall(r"^[PDWC] ", compact, re.M)), 6)
            self.assertRegex(normal, r"W seat5 Plus 1x\s+—\s+███████░░░  70%\s+7d\s+○ idle")
            self.assertRegex(normal, r"C claude tier \?\s+███████▌░░  75%\s+██████░░░░  60%\s+7d\s")
            self.assertRegex(compact, r"W seat5 Plus 1x\s+—\s+███▌░  70%\s+7d\s+○ idle")

    def test_remaining_capacity_names_only_the_binding_reported_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = 1_784_250_000.0
            self.healthy_pool(root, settings, current)
            with Ledger(root) as data:
                data["seats"]["seat1"]["last_probe_limits"] = {
                    "5h": {
                        "pct": 25,
                        "resets_at": current + 2 * 3600,
                        "window_minutes": 300,
                    },
                    "weekly": {
                        "pct": 80,
                        "resets_at": current + 6 * 86400,
                        "window_minutes": 10080,
                    },
                }

            rendered = dashboard(root, settings, [], color=False, width=160, at=current)
            seat_line = next(
                line for line in rendered.splitlines() if re.match(r"^P\s+seat1", line)
            )
            # Both windows are visible; RESET follows the binding (5-hour) window.
            self.assertRegex(seat_line, r"██▌░░░░░░░  25%\s+████████░░  80%\s+2h\s")

    def test_narrow_layout_prioritizes_current_work_without_cryptic_background_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = 1_784_250_000.0
            self.healthy_pool(root, settings, current)
            with Ledger(root) as data:
                data["seats"]["seat1"]["in_flight"] = {
                    "dispatcher_pid": os.getpid(),
                    "kind": "implementation",
                    "label": "repair dashboard",
                    "started_at": iso(current - 9),
                }

            rendered = dashboard(
                root, settings, [], color=False, width=50, height=14, at=current
            )
            self.assertIn("P seat1 Plus 1x", rendered)
            self.assertRegex(rendered, r"P seat1 Plus 1x\s+7d ████▌  90%  ● repair")
            self.assertIn("ONGOING WORK", rendered)
            self.assertNotIn("BACKGROUND WORK", rendered)
            self.assertNotIn(" BG ", rendered)
            self.assertTrue(all(self.display_width(line) <= 50 for line in rendered.splitlines()))

    def test_every_width_fits_and_colour_only_adds_escapes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = 1_784_250_000.0
            self.healthy_pool(root, settings, current)
            with Ledger(root) as data:
                data["seats"]["seat1"]["last_probe_limits"]["5h"] = {
                    "pct": 12.5,
                    "resets_at": current + 3 * 3600,
                    "window_minutes": 300,
                }
                data["seats"]["seat2"]["probed_at"] = iso(current - 11 * 3600)
                data["seats"]["seat2"]["usage_updated_at"] = iso(current - 11 * 3600)
            # 100–106 columns once overflowed through the AGE column.
            for width in range(20, 161):
                plain = dashboard(root, settings, [], color=False, width=width, at=current)
                colored = dashboard(root, settings, [], color=True, width=width, at=current)
                self.assertEqual(self.ansi.sub("", colored), plain, width)
                self.assertTrue(
                    all(self.display_width(line) <= width for line in plain.splitlines()),
                    f"{width}\n{plain}",
                )
            wide = dashboard(root, settings, [], color=False, width=120, at=current)
            # Threshold colours: seat1's 5-hour window is low, so its bar is red.
            colored = dashboard(root, settings, [], color=True, width=120, at=current)
            seat1 = next(line for line in colored.splitlines() if line.startswith("P seat1"))
            self.assertIn("\033[38;2;255;85;85m█▎", seat1)
            self.assertRegex(wide, r"P seat1 Plus 1x\s+█▎░+  12%\s+█████████░  90%\s+3h\s")
            self.assertRegex(wide, r"W seat2 Plus 1x .*small\+big  11\.0h")
            # Roles are primary and worker only; a legacy deputy reads as a worker.
            self.assertNotRegex(wide, r"(?m)^D ")
            self.assertIn("CODEX 5 · primary seat1 is used last", wide)
            with mock.patch("codex_seats.display.derive_roles", return_value={"seat2": "deputy"}):
                self.assertEqual(role_badges(settings)["seat2"], "W")

    def test_sparse_color_keeps_plain_symbols_and_never_wraps_a_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = 1_784_250_000.0
            self.healthy_pool(root, settings, current)
            plain = dashboard(root, settings, [], color=False, width=100, at=current)
            colored = dashboard(root, settings, [], color=True, width=100, at=current)
            self.assertEqual(self.ansi.sub("", colored), plain)
            self.assertIn("█", plain)
            self.assertIn("○ idle", plain)
            self.assertIn("Ready 6/6", plain)
            disabled = dashboard(root, registry_settings(), [], color=False)
            self.assertIn("– off", disabled)
            disabled_line = next(
                line for line in disabled.splitlines() if "claude" in line
            )
            self.assertIn("—", disabled_line)
            provider_lines = [
                line
                for line in colored.splitlines()
                if re.match(r"^(P|W|C)\s", line)
            ]
            self.assertTrue(provider_lines)
            self.assertTrue(all(not line.startswith("\033[") for line in provider_lines))
            self.assertTrue(
                all(len(self.ansi.sub("", line)) <= 100 for line in colored.splitlines())
            )

    def test_weekly_only_ignores_inactive_history_and_reports_exact_fit_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = time.time()
            self.healthy_pool(root, settings, current)
            settings["pool"]["task_min_headroom_pct_by_seat"] = {
                "seat2": {"small": 95, "big": 10},
                "seat3": {"small": 95, "big": 95},
            }
            with Ledger(root) as data:
                seat1 = data["seats"]["seat1"]
                seat1["usage_reset_windows"] = {
                    "monthly": {"resets_at": current + 30 * 86400}
                }
                seat1["usage_cycles"].update(
                    {
                        "weekly": {"observed_resets": 1},
                        "monthly": {"observed_resets": 4, "previous_cycles": [{"x": 1}]},
                        "5h": {"observed_resets": 9},
                    }
                )
                data["seats"]["seat4"]["account_window_observation"].update(
                    {"status": "failed", "last_attempt_observed_at": current}
                )
                data["seats"]["seat5"]["active_window_observation"] = None
                data["seats"]["seat5"].update(
                    {
                        "status": "needs-login",
                        "auth_failure": "expired",
                        "usage_updated_at": None,
                        "probed_at": None,
                    }
                )

            rendered = dashboard(root, settings, [], color=False, width=120, at=current)
            machine = json.loads(status_json(root, settings))

            self.assertIn("Ready 3/6", rendered)
            fit_values = {
                re.split(r"\s+", line.strip())[-2]
                for line in rendered.splitlines()
                if re.match(r"^(P|W)\s", line)
            }
            self.assertTrue({"small+big", "small", "big", "none"}.issubset(fit_values))
            self.assertIn("× needs sign-in", rendered)
            unhealthy_line = next(
                line for line in rendered.splitlines() if "seat5" in line
            )
            self.assertIn("—", unhealthy_line)
            self.assertNotIn("missing reset window", rendered)
            self.assertNotIn("m 30d", rendered)
            self.assertEqual(set(machine["seats"]["seat1"]["usage_cycles"]), {"weekly"})
            self.assertEqual(
                machine["seats"]["seat1"]["inactive_historical_window_names"],
                ["5h", "monthly"],
            )
            self.assertNotIn(
                "previous_cycles",
                json.dumps(machine["seats"]["seat1"]["inactive_historical_window_names"]),
            )
            forensic = dashboard(
                root, settings, [], color=False, width=120, at=current, forensic=True
            )
            self.assertIn("observed=w×1", forensic)
            self.assertNotIn("observed=5h", forensic)
            self.assertNotIn("m×4", forensic)

    def test_width_bounding_sanitizes_controls_and_counts_wide_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = 1_784_250_000.0
            self.healthy_pool(root, settings, current)
            with Ledger(root) as data:
                data["jobs"].append(
                    {
                        "ts": iso(current - 1),
                        "seat": "seat1",
                        "provider": "codex",
                        "status": "completed",
                        "label": "\033[31m" + "界" * 100 + "\nINJECTED",
                        "model": "gpt-test",
                        "tokens": 1000,
                    }
                )

            rendered = dashboard(
                root,
                settings,
                ["wide warning " + "界" * 100],
                color=False,
                width=80,
                at=current,
            )

            self.assertNotIn("\033", rendered)
            self.assertNotIn("\nINJECTED", rendered)
            self.assertTrue(
                all(self.display_width(line) <= 80 for line in rendered.splitlines())
            )

    def test_balance_deltas_last_for_one_completed_probe_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = 1_784_250_000.0
            self.healthy_pool(root, settings, current)
            state = DashboardViewState()
            initial = dashboard(
                root,
                settings,
                [],
                color=False,
                width=120,
                refresh_snapshot=None,
                view_state=state,
                at=current,
            )
            self.assertNotRegex(initial, r"[▲▼]\d")
            with Ledger(root) as data:
                data["seats"]["seat1"]["pct_left"] = 89
                data["seats"]["seat1"]["last_probe_limits"]["weekly"]["pct"] = 89
            generation_one = SimpleNamespace(completion_generation=1)
            changed = dashboard(
                root,
                settings,
                [],
                color=False,
                width=120,
                refresh_snapshot=generation_one,
                view_state=state,
                at=current,
            )
            self.assertIn("▼1", changed)
            redrawn = dashboard(
                root,
                settings,
                [],
                color=False,
                width=120,
                refresh_snapshot=generation_one,
                view_state=state,
                at=current,
            )
            self.assertIn("▼1", redrawn)
            expired = dashboard(
                root,
                settings,
                [],
                color=False,
                width=120,
                refresh_snapshot=SimpleNamespace(completion_generation=2),
                view_state=state,
                at=current,
            )
            self.assertNotIn("▼1", expired)

    def test_attention_covers_staleness_failures_and_missing_resets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = 1_784_250_000.0
            self.healthy_pool(root, settings, current)
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                seat.update(
                    {
                        "status": "capped",
                        "usage_updated_at": iso(current - 1000),
                        "usage_source": "telemetry",
                        "last_probe_limits": {},
                        "auth_failure": "expired token",
                    }
                )
            refresh = SimpleNamespace(
                completion_generation=1,
                last_result="Codex 4/5; Claude failed: auth",
            )
            rendered = dashboard(
                root,
                settings,
                ["settings warning"],
                color=False,
                width=120,
                refresh_snapshot=refresh,
                probe_interval=60,
                at=current,
            )
            self.assertIn("ATTENTION", rendered)
            self.assertIn("! seat1 authentication failed · sign in again", rendered)
            self.assertIn("3 more (--forensic)", rendered)
            self.assertIn("The last refresh failed · check the result", rendered)
            self.assertNotIn("Next:", rendered)
            self.assertIn("settings warning", rendered)
            forensic = dashboard(
                root, settings, [], color=False, forensic=True, width=180,
                refresh_snapshot=refresh, at=current
            )
            self.assertIn("capacity unknown", forensic)
            self.assertIn("source=telemetry", forensic)
            self.assertIn("provider capped", forensic)
            self.assertIn("authentication failed: expired token", forensic)
            self.assertIn("has no authoritative reset window", forensic)

    def test_settings_warnings_collapse_normally_and_expand_forensically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = 1_784_250_000.0
            self.healthy_pool(root, settings, current)
            warnings = [
                "unknown setting 'execution.future_coordination_hint' was preserved; check it",
                "unknown setting 'projects.local.future_branch_policy' was preserved; check it",
            ]

            normal = dashboard(
                root, settings, warnings, color=False, width=180, at=current
            )
            forensic = dashboard(
                root,
                settings,
                warnings,
                color=False,
                forensic=True,
                width=180,
                at=current,
            )

            self.assertIn(
                "Settings file has 2 unrecognised keys (kept) · run dash --forensic to see them",
                normal,
            )
            self.assertNotIn("future_coordination_hint' was preserved", normal)
            self.assertIn("future_coordination_hint' was preserved", forensic)
            self.assertIn("projects.local.future_branch_policy' was preserved", forensic)

    def test_active_and_history_preserve_metrics_collapse_and_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = 1_784_250_000.0
            self.healthy_pool(root, settings, current)
            with Ledger(root) as data:
                data["seats"]["seat1"]["in_flight"] = {
                    "dispatcher_pid": os.getpid(),
                    "kind": "implementation",
                    "label": "build the signal table with a deliberately long label",
                    "tokens": 12_500,
                    "context_pct": 44,
                    "started_at": iso(current - 65),
                }
                data["providers"]["claude"]["in_flight"] = {
                    "dispatcher_pid": os.getpid(),
                    "kind": "review",
                    "label": "review the result",
                }
                statuses = ["completed", "failed", "blocked", "canceled", "completed", "failed"]
                for index, status in enumerate(statuses):
                    data["jobs"].append(
                        {
                            "ts": iso(current - 60 * (index + 2)),
                            "seat": "seat2",
                            "provider": "codex",
                            "status": status,
                            "label": f"history label {index} " + "x" * 100,
                            "model": "gpt-test",
                            "tokens": 1000 + index,
                        }
                    )
                duplicate = {
                    "ts": iso(current - 30),
                    "seat": "seat1",
                    "provider": "codex",
                    "status": "completed",
                    "label": "same result",
                    "model": "gpt-test",
                    "tokens": 2000,
                }
                data["jobs"].extend(
                    [
                        {**duplicate, "ts": iso(current - 20)},
                        {**duplicate, "ts": iso(current - 10)},
                        {
                            **duplicate,
                            "ts": iso(current - 5),
                            "status": "running",
                            "label": "must not appear",
                        },
                    ]
                )
            rendered = dashboard(root, settings, [], color=False, width=160, at=current)
            self.assertIn("ONGOING WORK", rendered)
            active_line = next(line for line in rendered.splitlines() if "build the signal table" in line and "Running" in line)
            self.assertIn("12k", active_line)
            self.assertIn("context 44%", active_line)
            self.assertIn("1m05s", active_line)
            claude_active = next(
                line for line in rendered.splitlines() if "review the result" in line and "Running" in line
            )
            self.assertIn("tokens ?", claude_active)
            self.assertIn("context ?", claude_active)
            self.assertNotIn("must not appear", rendered)
            self.assertIn("×2", rendered)
            history_lines = re.split(r"RECENT ─+ last 5 ──\n", rendered, maxsplit=1)[1].splitlines()
            self.assertEqual(len(history_lines), 5)
            self.assertTrue(all("gpt-test" in line for line in history_lines))
            self.assertIn("× failed", rendered)
            self.assertIn("! blocked", rendered)
            self.assertTrue(all(len(line) <= 160 for line in rendered.splitlines()))

    def test_forensic_retains_hidden_default_fields_at_compact_width(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = 1_784_250_000.0
            self.healthy_pool(root, settings, current)
            normal = dashboard(root, settings, [], color=False, width=80, at=current)
            forensic = dashboard(
                root, settings, [], color=False, forensic=True, width=80, at=current
            )
            self.assertNotIn("reserve=", normal)
            self.assertNotIn("last pool job=", normal)
            self.assertNotIn("source=account", normal)
            self.assertIn("FORENSIC", forensic)
            self.assertIn("reserve=", forensic)
            self.assertIn("source=account", forensic)
            self.assertIn("last pool job=", forensic)
            self.assertIn("raw=", forensic)
            self.assertIn("observed=", forensic)
            self.assertTrue(all(len(line) <= 80 for line in forensic.splitlines()))

    def test_height_bounds_watch_frames_and_leaves_one_shot_unbounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = time.time()
            self.healthy_pool(root, settings, current)
            with Ledger(root) as data:
                data["seats"]["seat1"]["in_flight"] = {
                    "dispatcher_pid": os.getpid(),
                    "kind": "implementation",
                    "label": "visible live work",
                    "started_at": iso(current - 10),
                }
                for index in range(8):
                    data["jobs"].append(
                        {
                            "ts": iso(current - index),
                            "seat": "seat2",
                            "provider": "codex",
                            "status": "completed",
                            "label": f"history {index}",
                        }
                    )
            jobs = [
                {
                    "request_id": "a" * 32,
                    "state": "running",
                    "provider": "codex",
                    "seat": "seat3",
                    "kind": "review",
                    "size": "small",
                    "elapsed_seconds": 4,
                    "label": "visible background work",
                }
            ]
            version_warning = "WARNING: Codex executable unhealthy: missing"
            warnings = [version_warning, *(f"warning {index}" for index in range(8))]
            with mock.patch(
                "codex_seats.display.dispatch_client.live_jobs", return_value=jobs
            ):
                bounded = dashboard(
                    root, settings, warnings, color=False, width=80, height=24,
                    forensic=True, at=current
                )
                unbounded = dashboard(
                    root, settings, warnings, color=False, width=80,
                    forensic=True, at=current
                )
                tiny = dashboard(
                    root, settings, warnings, color=False, width=80, height=3,
                    forensic=True, at=current
                )
                single = dashboard(
                    root, settings, warnings, color=False, width=80, height=1,
                    forensic=True, at=current
                )

            self.assertLessEqual(len(bounded.splitlines()), 24)
            self.assertIn("ONGOING WORK", bounded)
            self.assertIn(version_warning, bounded)
            self.assertRegex(bounded, r"(?:omitted|RESIZE)")
            self.assertGreater(len(unbounded.splitlines()), 24)
            self.assertLessEqual(len(tiny.splitlines()), 3)
            self.assertIn("RESIZE", tiny)
            self.assertEqual(len(single.splitlines()), 1)
            self.assertIn("RESIZE", single)

    def test_height_floor_explicitly_preserves_attention_and_background(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = time.time()
            self.healthy_pool(root, settings, current)
            warnings = [f"warning {index}" for index in range(8)]

            bounded = dashboard(
                root,
                settings,
                warnings,
                color=False,
                width=80,
                height=22,
                at=current,
            )

            self.assertLessEqual(len(bounded.splitlines()), 22)
            self.assertIn("ATTENTION", bounded)
            self.assertIn("warning 0", bounded)
            self.assertRegex(bounded, r"ONGOING WORK ─+\n  none")
            self.assertIn("omitted", bounded)

    def test_claude_fit_matches_status_json_for_busy_and_unhealthy_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            current = time.time()
            self.healthy_pool(root, settings, current)
            with Ledger(root) as data:
                data["providers"]["claude"]["in_flight"] = {
                    "dispatcher_pid": os.getpid(),
                    "kind": "review",
                    "label": "busy but capacity remains visible",
                }
            busy_status = json.loads(status_json(root, settings))["claude"]
            busy_dashboard = dashboard(root, settings, [], color=False, width=120)
            busy_line = next(line for line in busy_dashboard.splitlines() if "claude" in line)
            expected = (
                "small+big" if busy_status["fits_big"] else "small"
                if busy_status["fits_small"] else "none"
            )
            self.assertIn(expected, busy_line)
            self.assertIn("● busy but capacity", busy_line)

            with Ledger(root) as data:
                data["providers"]["claude"].update(
                    {
                        "status": "ok",
                        "in_flight": None,
                        "last_error": "a prior task failed",
                    }
                )
            recovered_dashboard = dashboard(
                root, settings, [], color=False, width=120
            )
            recovered_line = next(
                line for line in recovered_dashboard.splitlines() if "claude" in line
            )
            self.assertIn("○ idle", recovered_line)
            self.assertIn(expected, recovered_line)
            self.assertIn("provider reported an error", recovered_dashboard)
            recovered_forensic = dashboard(
                root, settings, [], color=False, forensic=True, width=180
            )
            self.assertIn("provider error: a prior task failed", recovered_forensic)

            with Ledger(root) as data:
                data["providers"]["claude"].update(
                    {"status": "needs-auth", "in_flight": None}
                )
            unhealthy_status = json.loads(status_json(root, settings))["claude"]
            unhealthy_dashboard = dashboard(root, settings, [], color=False, width=120)
            unhealthy_line = next(
                line for line in unhealthy_dashboard.splitlines() if "claude" in line
            )
            self.assertFalse(unhealthy_status["fits_small"])
            self.assertFalse(unhealthy_status["fits_big"])
            self.assertIn("× needs sign-in", unhealthy_line)
            self.assertIn("—", unhealthy_line)

    def test_expired_claude_windows_are_not_displayed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "claude-usage.json").write_text(
                json.dumps(
                    {
                        "updated_at": 1,
                        "five_hour": {"used_percentage": 90, "resets_at": 10},
                    }
                )
            )
            self.assertEqual(load_claude_rate_limits(root, at=11), {})

    def test_claude_statusline_counts_confirmed_window_rollover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = Path(__file__).parents[1] / "claude-statusline.py"
            current = time.time()
            environment = dict(os.environ, CODEX_SEATS_ROOT=str(root))
            first = {
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 50,
                        "resets_at": current + 100,
                    }
                }
            }
            second = {
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 10,
                        "resets_at": current + 1000,
                    }
                }
            }
            for payload in (first, second):
                subprocess.run(
                    [sys.executable, str(script)],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    check=True,
                    env=environment,
                )
            cache = json.loads((root / "claude-usage.json").read_text())
            self.assertEqual(cache["usage_cycles"]["five_hour"]["observed_resets"], 1)


class LocalSeatDisplayTests(unittest.TestCase):
    """Configured local seats stay visible in a distinct, read-only section."""

    @staticmethod
    def local_settings(settings: dict, *, probe_output: str = '{"state":"ready","detail":"GPU idle"}') -> dict:
        settings["providers"]["local"]["mode"] = "opportunistic"
        settings["local_hosts"] = {
            "studio": {
                "label": "Studio",
                "lifecycle": "active",
                "resource_groups": {"gpu0": {"max_concurrency": 1}},
                "base_url": "http://127.0.0.1:8080/v1",
                "availability_probe": {
                    "argv": [sys.executable, "-c", f"print({probe_output!r})"],
                    "timeout_seconds": 2,
                },
            }
        }
        seat = {
            "label": "Coder",
            "lifecycle": "active",
            "mode": "opportunistic",
            "host": "studio",
            "resource_group": "gpu0",
            "model_profile": "qwen3.8-27b",
            "harness": "qwen",
            "capabilities": {"task_shapes": ["execute"], "validation": ["strong"]},
            "max_concurrency": 1,
            "runtime_fingerprint": "sha256:" + "a" * 64,
        }
        settings["local_seats"] = {
            "small": seat,
            "large": {**json.loads(json.dumps(seat)), "mode": "off", "model_profile": "qwen3.8-70b"},
        }
        return settings

    @staticmethod
    def filesystem_state(root: Path) -> dict:
        return {
            str(path.relative_to(root)): (
                path.is_dir(),
                path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in (root, *root.rglob("*"))
        }

    @staticmethod
    def local_line(rendered: str, name: str) -> str:
        # Rows are named "<model> on <host>"; tests address them as host/seat.
        # Narrow layouts may clip the host, so match on the model.
        _host, seat = name.split("/")
        model = {"small": "qwen3.8-27b", "large": "qwen3.8-70b"}[seat]
        return next(line for line in rendered.splitlines() if line.startswith(f"L {model} on"))

    def test_local_section_states_probe_age_history_label_and_no_writes(self):
        from codex_seats import local_seats

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.local_settings(registry_settings())
            current = 1_784_250_000.0
            DisplayTests.healthy_pool(root, settings, current)
            with Ledger(root) as data:
                data["jobs"].append(
                    {
                        "ts": iso(current - 30),
                        "seat": "small",
                        "provider": "local",
                        "status": "completed",
                        "label": "local history job",
                        "model": "qwen3.8-27b",
                        "tokens": 1500,
                    }
                )

            # Never probed: the enabled seat is unknown, the off seat is off, and
            # both remain listed. Ready counts enabled seats of every provider.
            unknown = dashboard(root, settings, [], color=False, width=120, at=current)
            self.assertIn("LOCAL 2 · ready 0/2 · global Opportunistic", unknown)
            self.assertIn("Ready 6/7", unknown)
            small = self.local_line(unknown, "studio/small")
            self.assertRegex(small, r"L qwen3.8-27b on studio\s+—\s+—\s+—\s+◐ not probed")
            large = self.local_line(unknown, "studio/large")
            self.assertIn("– off", large)
            history = next(line for line in unknown.splitlines() if "local history job" in line)
            self.assertIn("L:small", history)
            self.assertNotIn(" ? ", history)

            # A real bounded probe populates the cache; rendering reads it only.
            self.assertEqual(
                local_seats.probe_host(root, settings, "studio", clock=lambda: current)["state"],
                "ready",
            )
            before = self.filesystem_state(root)
            ready = dashboard(root, settings, [], color=False, width=120, at=current + 3)
            self.assertEqual(self.filesystem_state(root), before)
            self.assertIn("LOCAL 2 · ready 1/2", ready)
            self.assertIn("Ready 7/7", ready)
            small = self.local_line(ready, "studio/small")
            self.assertIn("○ ready", small)
            self.assertNotIn("GPU idle", small)
            self.assertRegex(small, r"\b3s\b")

            # Working is asserted only for a running local job on that seat.
            jobs = [
                {
                    "request_id": "a" * 32,
                    "state": "running",
                    "provider": "local",
                    "seat": "small",
                    "kind": "implementation",
                    "size": "small",
                    "elapsed_seconds": 4,
                    "label": "local live work",
                }
            ]
            with mock.patch("codex_seats.display.dispatch_client.live_jobs", return_value=jobs):
                working = dashboard(root, settings, [], color=False, width=120, at=current + 3)
            self.assertIn("● local live work", self.local_line(working, "studio/small"))
            self.assertNotIn("working", self.local_line(working, "studio/large"))
            self.assertIn("local/small", working.split("ONGOING WORK", 1)[1])

            # An expired observation is stale with its age, not silently unknown.
            stale = dashboard(root, settings, [], color=False, width=120, at=current + 40)
            small = self.local_line(stale, "studio/small")
            self.assertIn("◐ stale 40s", small)
            self.assertIn("~40s", small)
            self.assertIn(
                "! Local seat qwen3.8-27b on studio has not been probed for 40s · probe its host",
                stale,
            )

            # Global off keeps every seat visible but off, without any probe I/O.
            settings["providers"]["local"]["mode"] = "off"
            off = dashboard(root, settings, [], color=False, width=120, at=current + 3)
            self.assertIn("global Off", off)
            self.assertIn("– off", self.local_line(off, "studio/small"))
            self.assertIn("– off", self.local_line(off, "studio/large"))
            self.assertNotIn("Local seat", off)
            self.assertNotIn("ready", self.local_line(off, "studio/small"))

    def test_local_status_host_rows_share_seat_derivation(self):
        """The shared derivation yields host rows for the manager UI that agree
        with the dashboard's seat rows: stale carries age and last state,
        working comes only from running local jobs, gated hosts are off with
        no runtime cache I/O, and configuration_changed is unknown."""
        from codex_seats import local_seats
        from codex_seats.display import _local_seat_rows, _local_status

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.local_settings(registry_settings())
            current = 1_784_250_000.0
            DisplayTests.healthy_pool(root, settings, current)
            local_seats.probe_host(root, settings, "studio", clock=lambda: current)

            fresh = _local_status(root, settings, [], at=current + 3)
            host = fresh["hosts"]["studio"]
            self.assertEqual((host["key"], host["state"], host["style"]), ("ready", "○ available", "green"))
            self.assertEqual(host["detail"], "GPU idle")
            self.assertEqual((host["age"], host["age_text"]), (3.0, "3s"))
            self.assertEqual(host["observed_at"], current)
            self.assertEqual(host["working_seats"], [])
            self.assertEqual(fresh["seats"], _local_seat_rows(root, settings, [], at=current + 3))
            self.assertEqual([row["key"] for row in fresh["seats"]], ["off", "ready"])

            jobs = [{"state": "running", "provider": "local", "seat": "small", "label": "live work"}]
            working = _local_status(root, settings, jobs, at=current + 3)
            self.assertEqual(working["hosts"]["studio"]["key"], "working")
            self.assertEqual(working["hosts"]["studio"]["detail"], "small: live work")
            self.assertEqual(working["hosts"]["studio"]["working_seats"], ["small"])
            self.assertEqual([row["key"] for row in working["seats"]], ["off", "working"])

            stale = _local_status(root, settings, [], at=current + 40)
            host = stale["hosts"]["studio"]
            self.assertEqual((host["key"], host["state"], host["stale"]), ("stale", "~ stale", True))
            self.assertEqual((host["last_state"], host["detail"]), ("ready", "last ready"))
            self.assertEqual((host["age"], host["age_text"]), (40.0, "40s"))
            small = next(row for row in stale["seats"] if row["seat"] == "small")
            self.assertEqual((small["key"], small["age_text"], small["last_state"]), ("stale", "40s", "ready"))
            # The gated seat is off but still reports the host's observation age.
            large = next(row for row in stale["seats"] if row["seat"] == "large")
            self.assertEqual((large["key"], large["detail"], large["age_text"]), ("off", "seat mode off", "40s"))

            # configuration_changed is a known non-failure result: unknown, not degraded.
            settings["local_hosts"]["studio"]["label"] = "Renamed"
            changed = _local_status(root, settings, [], at=current + 3)
            self.assertEqual(changed["hosts"]["studio"]["reason"], "configuration_changed")
            self.assertEqual(changed["hosts"]["studio"]["key"], "unknown")
            self.assertEqual(changed["hosts"]["studio"]["detail"], "configuration changed")
            self.assertIsNone(changed["hosts"]["studio"]["observed_at"])
            small = next(row for row in changed["seats"] if row["seat"] == "small")
            self.assertEqual((small["key"], small["detail"]), ("unknown", "configuration changed"))
            rendered = dashboard(root, settings, [], color=False, width=120, at=current + 3)
            line = self.local_line(rendered, "studio/small")
            self.assertIn("◐ not probed", line)
            self.assertNotIn("degraded", line)
            settings["local_hosts"]["studio"]["label"] = "Studio"

            # Global off never touches the runtime cache, even with a cache present.
            settings["providers"]["local"]["mode"] = "off"
            with mock.patch.object(
                local_seats, "_runtime", side_effect=AssertionError("runtime cache read while off")
            ):
                off = _local_status(root, settings, [], at=current + 3)
            self.assertEqual(off["hosts"]["studio"]["key"], "off")
            self.assertEqual(off["hosts"]["studio"]["detail"], "global mode off")
            self.assertIsNone(off["hosts"]["studio"]["observed_at"])
            self.assertEqual({row["key"] for row in off["seats"]}, {"off"})

            # A host with an ineligible lifecycle is off with its lifecycle named.
            settings["providers"]["local"]["mode"] = "opportunistic"
            settings["local_hosts"]["studio"]["lifecycle"] = "draining"
            draining = _local_status(root, settings, [], at=current + 3)
            self.assertEqual(draining["hosts"]["studio"]["key"], "off")
            self.assertEqual(draining["hosts"]["studio"]["detail"], "host draining")

    def test_local_probe_states_and_degraded_failures_render_distinctly(self):
        from codex_seats import local_seats

        for output, expected in (
            ('{"state":"gaming","detail":"GPU busy with a game"}', "◐ gaming"),
            ('{"state":"asleep"}', "○ asleep"),
            ('{"state":"loading"}', "◐ loading"),
            ('{"state":"busy"}', "◐ busy"),
            ('{"state":"degraded","detail":"service down"}', "× degraded"),
            ("not json", "× degraded"),
        ):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = self.local_settings(registry_settings(), probe_output=output)
                current = 1_784_250_000.0
                DisplayTests.healthy_pool(root, settings, current)
                local_seats.probe_host(root, settings, "studio", clock=lambda: current)
                rendered = dashboard(root, settings, [], color=False, width=120, at=current + 2)
                small = self.local_line(rendered, "studio/small")
                self.assertIn(expected, small)
                self.assertIn("LOCAL 2 · ready 0/2", rendered)
                if output == "not json":
                    self.assertIn(
                        "! Local seat qwen3.8-27b on studio is degraded: probe output invalid",
                        rendered,
                    )
                elif "service down" in output:
                    self.assertIn("is degraded: service down · check the host", rendered)
                else:
                    self.assertNotIn("Local seat", rendered)

    def test_local_section_fits_narrow_widths_and_is_height_bounded_first(self):
        from codex_seats import local_seats

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.local_settings(registry_settings())
            current = 1_784_250_000.0
            DisplayTests.healthy_pool(root, settings, current)
            local_seats.probe_host(root, settings, "studio", clock=lambda: current)
            with Ledger(root) as data:
                data["seats"]["seat1"]["in_flight"] = {
                    "dispatcher_pid": os.getpid(),
                    "kind": "implementation",
                    "label": "visible live work",
                    "started_at": iso(current - 10),
                }

            for width in (90, 50):
                rendered = dashboard(root, settings, [], color=False, width=width, at=current + 3)
                self.assertIn("LOCAL 2", rendered)
                self.assertIn("L qwen3.8-27b on", rendered)
                self.assertIn("○ ready", self.local_line(rendered, "studio/small"))
                self.assertIn("off", self.local_line(rendered, "studio/large"))
                self.assertTrue(
                    all(DisplayTests.display_width(line) <= width for line in rendered.splitlines()),
                    rendered,
                )
            forensic = dashboard(
                root, settings, [], color=False, width=120, forensic=True, at=current + 3
            )
            self.assertIn("L:small host=studio/gpu0", forensic)
            self.assertIn("global opportunistic, seat opportunistic", forensic)

            unbounded = dashboard(root, settings, [], color=False, width=120, at=current + 3)
            self.assertIn("LOCAL 2", unbounded)
            bounded = dashboard(
                root, settings, [], color=False, width=120, height=13, at=current + 3
            )
            self.assertLessEqual(len(bounded.splitlines()), 13)
            self.assertIn("ONGOING WORK", bounded)
            self.assertIn("visible live work", bounded)
            self.assertNotIn("LOCAL 2", bounded)
            self.assertIn("omitted", bounded)


if __name__ == "__main__":
    unittest.main()
