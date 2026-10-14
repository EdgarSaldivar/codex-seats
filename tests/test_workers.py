import json
import argparse
import os
import tempfile
import time
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from codex_seats.state import Ledger
from registry_fixture import registry_settings, write_settings
from codex_seats import cli
from codex_seats.workers import (
    claude_cli_error_text,
    codex_cli_error_text,
    probe_is_stale,
    probe_seats,
)
from codex_seats.provider_claude import (
    claude_model_candidates,
    claude_task_fits,
    effort_for_kind,
    run_claude,
)
from codex_seats.provider_codex import run_codex
from codex_seats.router import choose_provider
from codex_seats.worker_config import (
    persisted_config_payload,
    static_config_payload,
    sync_codex_config,
)


def test_structured_probe_uses_account_read_without_terminal_or_config(tmp_path):
    settings = write_settings(tmp_path, registry_settings())
    with (
        mock.patch("codex_seats.watch.acquire_refresh_lock", return_value=17) as acquire,
        mock.patch("codex_seats.watch.release_refresh_lock") as release,
        mock.patch("codex_seats.account.refresh_account_usage", return_value=["seat1 account -> ok"]) as refresh,
        mock.patch("codex_seats.workers.sync_codex_config", side_effect=AssertionError("config sync used")),
        mock.patch("codex_seats.workers.resolve_executable", side_effect=AssertionError("terminal launch used")),
    ):
        assert probe_seats(tmp_path, settings, ["seat1"]) == ["seat1 account -> ok"]
    acquire.assert_called_once()
    release.assert_called_once_with(17)
    refresh.assert_called_once_with(tmp_path, settings, ["seat1"])


def test_structured_probe_busy_lock_skips_read_and_preserves_cached_capacity(tmp_path):
    settings = write_settings(tmp_path, registry_settings())
    with Ledger(tmp_path) as data:
        _set_codex_capacity(data["seats"]["seat1"], 71)
    with (
        mock.patch("codex_seats.watch.acquire_refresh_lock", return_value=None),
        mock.patch("codex_seats.account.refresh_account_usage", side_effect=AssertionError("duplicate refresh")),
    ):
        assert "another usage refresh" in probe_seats(tmp_path, settings, ["seat1"])[0]
    with Ledger(tmp_path) as data:
        assert data["seats"]["seat1"]["pct_left"] == 71


def test_structured_probe_failed_read_keeps_last_capacity(tmp_path):
    settings = write_settings(tmp_path, registry_settings())
    with Ledger(tmp_path) as data:
        _set_codex_capacity(data["seats"]["seat1"], 64)
    with (
        mock.patch("codex_seats.watch.acquire_refresh_lock", return_value=18),
        mock.patch("codex_seats.watch.release_refresh_lock"),
        mock.patch("codex_seats.account.refresh_account_usage", return_value=["seat1 account unavailable"]),
    ):
        assert probe_seats(tmp_path, settings, ["seat1"]) == ["seat1 account unavailable"]
    with Ledger(tmp_path) as data:
        assert data["seats"]["seat1"]["pct_left"] == 64


def test_cli_probe_uses_structured_account_probe(tmp_path, capsys, monkeypatch):
    settings = write_settings(tmp_path, registry_settings())
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    with mock.patch("codex_seats.cli.probe_seats", return_value=["structured ok"]) as probe:
        assert cli.cmd_probe(argparse.Namespace(seat=None), settings) == 0
    probe.assert_called_once()
    assert "structured ok" in capsys.readouterr().out


def _set_codex_capacity(seat: dict, pct: float = 100, window: str = "monthly") -> None:
    observed = time.time()
    seat.update(
        {
            "status": "ok",
            "pct_left": pct,
            "probed_at": datetime.fromtimestamp(observed, timezone.utc).isoformat(),
            "usage_updated_at": datetime.fromtimestamp(observed, timezone.utc).isoformat(),
            "last_probe_limits": {
                window: {
                    "pct": pct,
                    "window_minutes": 43200 if window == "monthly" else 10080,
                }
            },
            "active_window_observation": {
                "source": "account",
                "complete": True,
                "observed_at": observed,
                "observed_windows": [window],
                "active_windows": [window],
            },
        }
    )


class ClaudeHardOffTests(unittest.TestCase):
    def test_hard_off_returns_before_executable_lookup(self):
        settings = registry_settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, settings)
            with mock.patch("codex_seats.workers.resolve_executable", side_effect=AssertionError("must not probe")):
                result = run_claude(
                    root,
                    settings,
                    {"prompt": "review this", "kind": "review", "provider": "claude"},
                )
        self.assertEqual(result.status, "disabled")
        self.assertEqual(result.code, 4)

    def test_auto_routing_ignores_claude_when_off(self):
        settings = registry_settings()
        with tempfile.TemporaryDirectory() as directory:
            with Ledger(Path(directory)) as data:
                provider, reason = choose_provider("auto", "review", settings, data)
        self.assertEqual(provider, "codex")
        self.assertIsNone(reason)

    def test_opportunistic_routing_is_kind_bounded(self):
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        limits = {
            "updated_at": time.time(),
            "five_hour": {"remaining_percentage": 80},
            "seven_day": {"remaining_percentage": 57},
        }
        with tempfile.TemporaryDirectory() as directory:
            with Ledger(Path(directory)) as data:
                data["providers"]["claude"]["status"] = "ok"
                review_provider = choose_provider(
                    "auto", "review", settings, data, claude_limits=limits
                )[0]
                implementation_provider = choose_provider(
                    "auto",
                    "implementation",
                    settings,
                    data,
                    size="big",
                    claude_limits=limits,
                )[0]
                mechanical_provider = choose_provider(
                    "auto", "mechanical", settings, data, claude_limits=limits
                )[0]
                planning_provider = choose_provider(
                    "auto", "planning", settings, data, size="big", claude_limits=limits
                )[0]
        # Config-driven preference (execution.prefer_provider_by_kind): the quality
        # lens (review/planning) prefers Claude; build kinds (implementation,
        # mechanical) prefer the abundant Codex pool.
        self.assertEqual(review_provider, "claude")
        self.assertEqual(implementation_provider, "codex")
        self.assertEqual(mechanical_provider, "codex")
        self.assertEqual(planning_provider, "claude")

    def test_task_fit_selects_capability_descending_model_chains(self):
        settings = registry_settings()
        self.assertEqual(
            claude_model_candidates(settings, "security", "big"),
            ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5"],
        )
        self.assertEqual(
            claude_model_candidates(settings, "implementation", "big"),
            ["claude-opus-5-5", "claude-sonnet-5"],
        )
        self.assertEqual(
            claude_model_candidates(settings, "docs", "small"),
            ["claude-sonnet-5"],
        )
        self.assertEqual(
            claude_model_candidates(settings, "debug", "small"),
            ["claude-opus-5-5", "claude-sonnet-5"],
        )

    def test_fable_specific_cap_falls_through_to_opus_without_global_cooldown(self):
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = []

            def fake_execute(command, output, *_args, **_kwargs):
                commands.append(command)
                model = command[command.index("--model") + 1]
                if "fable" in model:
                    payload = {
                        "type": "result",
                        "subtype": "error",
                        "is_error": True,
                        "result": "You've reached your Fable 5 limit.",
                    }
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps(payload))
                    return "failed", json.dumps(payload), 1, 1
                payload = {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "structured_output": {
                        "status": "completed",
                        "summary": "done",
                        "changed_files": [],
                        "test_evidence": ["fixture"],
                        "warnings": [],
                        "needs_controller": None,
                    },
                }
                output.write_text(json.dumps(payload))
                return "completed", json.dumps(payload), 0, 1

            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/claude"),
                mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                mock.patch("codex_seats.workers._execute", side_effect=fake_execute),
            ):
                result = run_claude(
                    root,
                    settings,
                    {
                        "prompt": "audit the boundary",
                        "kind": "security",
                        "size": "big",
                        "_capacity_checked": True,
                    },
                )
            self.assertEqual(result.status, "completed")
            self.assertEqual(
                [command[command.index("--model") + 1] for command in commands],
                ["claude-fable-5-1", "claude-opus-5-5"],
            )
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                self.assertEqual(provider["status"], "ok")
                self.assertEqual(
                    provider["models"]["claude-fable-5-1"]["status"], "capped"
                )

    def test_busy_claude_spills_automatic_implementation_to_codex(self):
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        limits = {"seven_day": {"remaining_percentage": 57}}
        with tempfile.TemporaryDirectory() as directory:
            with Ledger(Path(directory)) as data:
                data["providers"]["claude"]["status"] = "ok"
                data["providers"]["claude"]["in_flight"] = {
                    "dispatcher_pid": os.getpid(),
                }
                provider, _ = choose_provider(
                    "auto",
                    "implementation",
                    settings,
                    data,
                    size="big",
                    claude_limits=limits,
                )
        self.assertEqual(provider, "codex")

    def test_low_claude_headroom_falls_back_to_codex_for_auto_work(self):
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        limits = {
            "five_hour": {"remaining_percentage": 50},
            "seven_day": {"remaining_percentage": 20},
        }
        with tempfile.TemporaryDirectory() as directory:
            with Ledger(Path(directory)) as data:
                data["providers"]["claude"]["status"] = "ok"
                provider, _ = choose_provider(
                    "auto",
                    "audit",
                    settings,
                    data,
                    size="big",
                    claude_limits=limits,
                )
        self.assertEqual(provider, "codex")

    def test_explicit_low_capacity_claude_requires_override(self):
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        limits = {
            "updated_at": time.time(),
            "five_hour": {"remaining_percentage": 14},
            "seven_day": {"remaining_percentage": 14},
        }
        with tempfile.TemporaryDirectory() as directory:
            with Ledger(Path(directory)) as data:
                provider, reason = choose_provider(
                    "claude",
                    "audit",
                    settings,
                    data,
                    size="big",
                    claude_limits=limits,
                )
                forced = choose_provider(
                    "claude",
                    "audit",
                    settings,
                    data,
                    size="big",
                    claude_limits=limits,
                    allow_reserve=True,
                )[0]
        self.assertEqual(provider, "unavailable")
        self.assertIn("14% headroom", reason or "")
        self.assertEqual(forced, "claude")

    def test_unknown_claude_capacity_allows_small_probe_but_not_big_work(self):
        settings = registry_settings()
        self.assertEqual(claude_task_fits(settings, "small", {})[0], True)
        self.assertEqual(claude_task_fits(settings, "big", {})[0], False)

    def test_big_claude_work_requires_fresh_short_and_weekly_windows(self):
        settings = registry_settings()
        complete = {
            "updated_at": time.time(),
            "five_hour": {"remaining_percentage": 80},
            "seven_day": {"remaining_percentage": 60},
        }
        self.assertTrue(claude_task_fits(settings, "big", complete)[0])
        self.assertFalse(claude_task_fits(settings, "big", {"updated_at": time.time(), "seven_day": complete["seven_day"]})[0])
        stale = dict(complete, updated_at=time.time() - 7 * 3600)
        self.assertFalse(claude_task_fits(settings, "big", stale)[0])

    def test_busy_claude_never_appends_to_historical_queue(self):
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, settings)
            with Ledger(root) as data:
                data["pending_queue"] = [{"prompt": "historical"}]
                data["providers"]["claude"]["in_flight"] = {
                    "dispatcher_pid": os.getpid(),
                    "child_pid": None,
                }
            result = run_claude(
                root,
                settings,
                {
                    "prompt": "review",
                    "provider": "claude",
                    "kind": "review",
                    "_capacity_checked": True,
                },
            )
            with Ledger(root) as data:
                self.assertEqual(data["pending_queue"], [{"prompt": "historical"}])
        self.assertEqual(result.status, "unavailable")

    def test_exhausted_codex_never_appends_to_historical_queue(self):
        settings = registry_settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, settings)
            with Ledger(root) as data:
                data["pending_queue"] = [{"prompt": "historical"}]
            result = run_codex(
                root,
                settings,
                {"prompt": "implement", "kind": "implementation"},
            )
            with Ledger(root) as data:
                self.assertEqual(data["pending_queue"], [{"prompt": "historical"}])
        self.assertEqual(result.status, "unavailable")

    def test_delegated_codex_selection_never_runs_interactive_probe(self):
        settings = registry_settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "seat3"
            home.mkdir()
            (home / "auth.json").write_text("{}")
            (home / "config.toml").write_text("")
            write_settings(root, settings)
            with Ledger(root) as data:
                _set_codex_capacity(data["seats"]["seat3"])
                data["seats"]["seat3"]["probed_at"] = None

            def fake_execute(*_args, **_kwargs):
                return "completed", "", 0, 1

            with (
                mock.patch(
                    "codex_seats.provider_codex.probe_seats",
                    side_effect=AssertionError("delegated run must not probe usage"),
                ),
                mock.patch(
                    "codex_seats.workers.resolve_executable", return_value="/fake/codex"
                ),
                mock.patch(
                    "codex_seats.workers.sync_codex_config", return_value=False
                ),
                mock.patch(
                    "codex_seats.workers.PowerLease", return_value=nullcontext()
                ),
                mock.patch("codex_seats.workers._execute", side_effect=fake_execute),
            ):
                result = run_codex(
                    root,
                    settings,
                    {
                        "prompt": "delegated work",
                        "kind": "mechanical",
                        "no_queue": True,
                        "no_probe": True,
                        "cd": str(root),
                    },
                )
        self.assertEqual(result.status, "completed")

    def test_forced_low_capacity_seat_rejects_big_task_before_launch(self):
        settings = registry_settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "seat5"
            home.mkdir()
            (home / "auth.json").write_text("{}")
            write_settings(root, settings)
            with Ledger(root) as data:
                _set_codex_capacity(data["seats"]["seat5"], 20, "weekly")
            with mock.patch(
                "codex_seats.workers.resolve_executable",
                side_effect=AssertionError("must reject before launch"),
            ):
                result = run_codex(
                    root,
                    settings,
                    {
                        "prompt": "large migration",
                        "kind": "implementation",
                        "size": "big",
                        "seat": 5,
                        "no_probe": True,
                    },
                )
        self.assertEqual(result.status, "reserved")
        self.assertIn("binding window", result.message)

    def test_forced_seat_uses_per_seat_floor_override(self):
        settings = registry_settings()
        settings["pool"]["task_min_headroom_pct_by_seat"] = {
            "seat5": {"small": 25, "big": 50}
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "seat5"
            home.mkdir()
            (home / "auth.json").write_text("{}")
            write_settings(root, settings)
            with Ledger(root) as data:
                _set_codex_capacity(data["seats"]["seat5"], 40, "weekly")
            with mock.patch(
                "codex_seats.workers.resolve_executable",
                side_effect=AssertionError("must reject before launch"),
            ):
                result = run_codex(
                    root,
                    settings,
                    {
                        "prompt": "large migration",
                        "kind": "implementation",
                        "size": "big",
                        "seat": 5,
                        "no_probe": True,
                    },
                )
        self.assertEqual(result.status, "reserved")
        self.assertIn("require 50%", result.message)

    def test_reserve_spend_runs_only_selected_seat_without_legacy_bypass(self):
        settings = registry_settings()
        settings["pool"]["small_task_min_headroom_pct"] = 5
        authority = {
            "version": 1,
            "seat": "seat2",
            "source": "current_user_request",
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            for seat_id in ("seat2", "seat3"):
                home = root / seat_id
                home.mkdir()
                (home / "auth.json").write_text("{}")
                (home / "config.toml").write_text("")
            write_settings(root, settings)
            with Ledger(root) as data:
                _set_codex_capacity(data["seats"]["seat2"], 13, "weekly")
                data["seats"]["seat2"]["last_probe_limits"]["5h"] = {
                    "pct": 100,
                    "window_minutes": 300,
                }
                _set_codex_capacity(data["seats"]["seat3"], 100, "weekly")

            def fake_execute(*_args, **_kwargs):
                return "completed", "", 0, 1

            launch_patches = (
                mock.patch(
                    "codex_seats.workers.resolve_executable", return_value="/fake/codex"
                ),
                mock.patch("codex_seats.workers.sync_codex_config", return_value=False),
                mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                mock.patch("codex_seats.workers._execute", side_effect=fake_execute),
            )
            with launch_patches[0], launch_patches[1], launch_patches[2], launch_patches[3]:
                result = run_codex(
                    root,
                    settings,
                    {
                        "prompt": "small bounded task",
                        "kind": "mechanical",
                        "size": "small",
                        "seat": "seat2",
                        "_reserve_spend_authority": authority,
                        "no_probe": True,
                        "cd": str(root),
                    },
                )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.worker, "seat2")
            with Ledger(root) as data:
                data["seats"]["seat2"]["in_flight"] = {
                    "job_id": "busy",
                    "dispatcher_pid": os.getpid(),
                }
            blocked = run_codex(
                root,
                settings,
                {
                    "prompt": "must not fall back",
                    "kind": "mechanical",
                    "size": "small",
                    "seat": "seat2",
                    "_reserve_spend_authority": authority,
                    "no_probe": True,
                },
            )
            self.assertEqual(blocked.status, "unavailable")
            self.assertEqual(blocked.worker, "seat2")

    def test_reserve_spend_preserves_active_head_exclusion(self):
        settings = registry_settings()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            write_settings(root, settings)
            with Ledger(root) as data:
                _set_codex_capacity(data["seats"]["seat2"], 100, "weekly")
            result = run_codex(
                root,
                settings,
                {
                    "prompt": "must not run on the head",
                    "kind": "mechanical",
                    "size": "small",
                    "seat": "seat2",
                    "_excluded_seats": ["seat2"],
                    "_reserve_spend_authority": {
                        "version": 1,
                        "seat": "seat2",
                        "source": "current_user_request",
                    },
                    "no_probe": True,
                },
            )
        self.assertEqual(result.status, "unavailable")
        self.assertIn("active-head", result.message)

    def test_forced_formerly_monthly_seat_gets_window_preflight(self):
        settings = registry_settings()
        settings["execution"]["capacity"]["fallback_burn_pct"]["big"] = 60
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "seat3"
            home.mkdir()
            (home / "auth.json").write_text("{}")
            (home / "config.toml").write_text("")
            write_settings(root, settings)
            with Ledger(root) as data:
                _set_codex_capacity(data["seats"]["seat3"], 50, "monthly")
            with mock.patch(
                "codex_seats.workers.resolve_executable",
                side_effect=AssertionError("must reject before launch"),
            ):
                result = run_codex(
                    root,
                    settings,
                    {
                        "prompt": "large migration",
                        "kind": "implementation",
                        "size": "big",
                        "seat": 3,
                        "no_probe": True,
                    },
                )
        self.assertEqual(result.status, "reserved")
        self.assertIn("binding window monthly", result.message)

    def test_post_probe_revalidates_formerly_monthly_seat(self):
        settings = registry_settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "seat3"
            home.mkdir()
            (home / "auth.json").write_text("{}")
            (home / "config.toml").write_text("")
            write_settings(root, settings)
            with Ledger(root) as data:
                _set_codex_capacity(data["seats"]["seat3"], 80, "monthly")
                data["seats"]["seat3"]["probed_at"] = "2020-01-01T00:00:00Z"

            def lower_capacity(probe_root, _settings, seats):
                self.assertEqual(seats, ["seat3"])
                with Ledger(probe_root) as data:
                    _set_codex_capacity(data["seats"]["seat3"], 5, "monthly")

            with (
                mock.patch(
                    "codex_seats.provider_codex.probe_seats",
                    side_effect=lower_capacity,
                ),
                mock.patch(
                    "codex_seats.workers.resolve_executable",
                    side_effect=AssertionError("must not launch after failed revalidation"),
                ),
            ):
                result = run_codex(
                    root,
                    settings,
                    {"prompt": "small task", "kind": "mechanical"},
                )
        self.assertEqual(result.status, "unavailable")


class FailureClassifierTests(unittest.TestCase):
    def test_revoked_refresh_token_quarantines_and_retry_routes_to_different_seat(self):
        settings = registry_settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings["pool"]["seats"]["seat1"]["home"] = str(root / "seat1")
            for seat_id in ["seat1", "seat2", "seat3", "seat4", "seat5"]:
                home = root / seat_id
                home.mkdir()
                (home / "auth.json").write_text("{}")
                (home / "config.toml").write_text("")
            write_settings(root, settings)
            with Ledger(root) as data:
                for seat in data["seats"].values():
                    _set_codex_capacity(seat)

            calls = []

            def fake_execute(_command, output, *_args, **_kwargs):
                calls.append(_args[4])
                output.parent.mkdir(parents=True, exist_ok=True)
                if len(calls) == 1:
                    message = "401 Unauthorized: refresh token was revoked"
                    output.write_text(json.dumps({"type": "error", "message": message}) + "\n")
                    return "failed", message, 1, 1
                output.write_text(json.dumps({"type": "turn.completed", "usage": {}}) + "\n")
                return "completed", "", 0, 1

            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"),
                mock.patch("codex_seats.workers.sync_codex_config", return_value=False),
                mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                mock.patch("codex_seats.workers._execute", side_effect=fake_execute),
            ):
                failed = run_codex(root, settings, {
                    "prompt": "first attempt", "seat": 1, "no_probe": True,
                })
                retried = run_codex(root, settings, {
                    "prompt": "campaign retry", "no_probe": True,
                })

            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.worker, "seat1")
            self.assertEqual(retried.status, "completed")
            self.assertNotEqual(retried.worker, "seat1")
            with Ledger(root) as data:
                self.assertEqual(data["seats"]["seat1"]["status"], "needs-login")
                self.assertIn("revoked", data["seats"]["seat1"]["auth_failure"])

    def test_codex_ignores_agent_transcript_for_provider_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "The task failed because application authentication hit quota 429.",
                        },
                    }
                )
                + "\n"
            )
            self.assertEqual(codex_cli_error_text(path, "fallback quota"), "")

    def test_codex_uses_top_level_transport_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text(json.dumps({"type": "error", "message": "usage limit reached"}) + "\n")
            self.assertIn("usage limit", codex_cli_error_text(path))

    def test_claude_ignores_successful_result_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            path.write_text(
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "Reviewed authentication and quota handling.",
                    }
                )
            )
            self.assertEqual(claude_cli_error_text(path, "fallback quota"), "")

    def test_zero_age_probe_is_fresh(self):
        with mock.patch("codex_seats.workers.age_h", return_value=0.0):
            self.assertFalse(probe_is_stale("now", 3600))

    def test_worker_config_is_mirrored_without_copying_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary"
            primary.mkdir()
            (primary / "config.toml").write_text('model = "test"\n')
            (primary / "deep.config.toml").write_text('model_reasoning_effort = "max"\n')
            (primary / "AGENTS.md").write_text("managed instructions\n")
            (primary / "auth.json").write_text("secret")
            settings = write_settings(root, registry_settings())
            settings["pool"]["seats"]["seat1"]["home"] = str(primary)
            self.assertTrue(sync_codex_config(root, settings, "seat2"))
            self.assertEqual((root / "seat2" / "config.toml").read_text(), 'model = "test"\n')
            self.assertEqual(
                (root / "seat2" / "deep.config.toml").read_text(),
                'model_reasoning_effort = "max"\n',
            )
            self.assertFalse((root / "seat2" / "auth.json").exists())
            self.assertEqual((root / "seat2" / "AGENTS.md").read_text(), "managed instructions\n")
            self.assertFalse(sync_codex_config(root, settings, "seat2"))

    def test_permissions_config_persists_a_valid_default(self):
        source = b'[permissions.controller]\ndescription = "automatic"\n'
        payload = persisted_config_payload(source)
        self.assertTrue(payload.startswith(b'default_permissions = "controller"\n'))
        self.assertEqual(persisted_config_payload(payload), payload)
        with self.assertRaisesRegex(ValueError, "must name a defined"):
            persisted_config_payload(
                b'default_permissions = "missing"\n[permissions.controller]\n'
            )

    def test_official_permission_defaults_coexist_with_custom_profiles(self):
        for builtin in (":read-only", ":workspace", ":danger-full-access"):
            source = (
                f'default_permissions = "{builtin}"\n'
                '[permissions.controller]\ndescription = "custom"\n'
            ).encode()
            self.assertEqual(persisted_config_payload(source), source)

    def test_primary_config_is_synchronized_to_another_registered_seat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "interactive"
            primary.mkdir()
            (primary / "config.toml").write_text(
                '[permissions.controller]\ndescription = "automatic"\n'
            )
            settings = write_settings(root, registry_settings(primary_home=primary))
            self.assertTrue(sync_codex_config(root, settings, "seat2"))
            copied = (root / "seat2" / "config.toml").read_text()
            self.assertIn('default_permissions = "controller"', copied)

    def test_worker_hook_trust_paths_are_rewritten_to_isolated_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary"
            primary.mkdir()
            (primary / "config.toml").write_text(
                f'[hooks.state."{primary}/config.toml:pre_tool_use:0:0"]\n'
                'trusted_hash = "sha256:test"\n'
            )
            settings = write_settings(root, registry_settings())
            settings["pool"]["seats"]["seat1"]["home"] = str(primary)
            sync_codex_config(root, settings, "seat2")
            copied = (root / "seat2" / "config.toml").read_text()
            self.assertIn(f'{root / "seat2"}/config.toml:pre_tool_use:0:0', copied)
            self.assertNotIn(f'{primary}/config.toml:pre_tool_use:0:0', copied)

    def test_dynamic_hook_cache_is_ignored_but_static_config_drift_is_not(self):
        left = (
            b'approval_policy = "never"\n'
            b'[hooks.state."/left/config.toml:pre_tool_use:0:0"]\n'
            b'trusted_hash = "sha256:left"\n'
            b'[features]\nplugins = false\n'
        )
        right = left.replace(b'/left/', b'/right/').replace(b'sha256:left', b'sha256:right')
        self.assertEqual(static_config_payload(left), static_config_payload(right))
        mutated = right.replace(b'plugins = false', b'plugins = true')
        self.assertNotEqual(static_config_payload(left), static_config_payload(mutated))

    def test_sync_propagates_reviewed_hook_hash_with_path_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary"
            primary.mkdir()
            source_path = f"{primary}/config.toml:pre_tool_use:0:0"
            (primary / "config.toml").write_text(
                f'[hooks.state."{source_path}"]\ntrusted_hash = "sha256:reviewed"\n'
            )
            settings = write_settings(root, registry_settings())
            settings["pool"]["seats"]["seat1"]["home"] = str(primary)
            sync_codex_config(root, settings, "seat3")
            copied = (root / "seat3" / "config.toml").read_text()
            self.assertIn("sha256:reviewed", copied)
            self.assertIn(f"{root / 'seat3'}/config.toml:pre_tool_use:0:0", copied)

    def test_kind_aware_effort_defaults(self):
        settings = registry_settings()
        self.assertEqual(effort_for_kind(settings, "mechanical"), "low")
        self.assertEqual(effort_for_kind(settings, "test"), "medium")
        self.assertEqual(effort_for_kind(settings, "implementation"), "high")
        self.assertEqual(effort_for_kind(settings, "planning"), "high")

    def test_codex_worker_command_receives_kind_effort_default(self):
        settings = registry_settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings["pool"]["seats"]["seat1"]["home"] = str(root / "seat1")
            for seat_id in ["seat1", "seat2", "seat3", "seat4", "seat5"]:
                home = root / seat_id
                home.mkdir()
                (home / "auth.json").write_text("{}")
                (home / "config.toml").write_text("")
            write_settings(root, settings)
            with Ledger(root) as data:
                for seat in data["seats"].values():
                    _set_codex_capacity(seat)
            captured = {}
            cleaned = []

            def fake_execute(command, *_args, **_kwargs):
                captured["command"] = command
                captured["env"] = _args[3]
                captured["schema"] = Path(
                    command[command.index("--output-schema") + 1]
                )
                captured["result"] = Path(
                    command[command.index("--output-last-message") + 1]
                )
                captured["launch_files"] = {
                    path.name for path in captured["schema"].parent.iterdir()
                }
                return "completed", "", 0, 1

            with (
                mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "secret-thread", "CODEX_CONTROL_SEAT": "seat3", "TMUX_PANE": "%1"}),
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"),
                mock.patch("codex_seats.workers.sync_codex_config", return_value=False),
                mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                mock.patch("codex_seats.workers._execute", side_effect=fake_execute),
                mock.patch(
                    "codex_seats.provider_codex.shutil.rmtree",
                    side_effect=lambda path, **_kwargs: cleaned.append(Path(path)),
                ),
            ):
                result = run_codex(
                    root,
                    settings,
                    {
                        "prompt": "rename this symbol",
                        "kind": "mechanical",
                        "seat": 3,
                        "no_probe": True,
                        "cd": str(root),
                        "write_scope": ["src/**"],
                    },
                )
            self.assertEqual(result.status, "completed")
            self.assertIn("model_reasoning_effort=low", captured["command"])
            joined = " ".join(captured["command"])
            self.assertIn('approval_policy="never"', joined)
            self.assertNotIn("approvals_reviewer", joined)
            self.assertIn('default_permissions="managed-', joined)
            self.assertNotIn("--sandbox", captured["command"])
            self.assertIn("--output-schema", captured["command"])
            # The seat config defines no MCP servers, so no disable override may
            # be emitted: on codex >= 0.144.5 disabling an undefined server
            # CREATES an invalid entry and config load aborts.
            self.assertNotIn("mcp_servers.managed_pool", joined)
            self.assertNotIn("CODEX_THREAD_ID", captured["env"])
            self.assertNotIn("CODEX_CONTROL_SEAT", captured["env"])
            self.assertNotIn("TMUX_PANE", captured["env"])
            self.assertEqual(captured["env"]["CODEX_HOME"], str(root / "seat3"))
            self.assertEqual(captured["env"]["HOME"], os.environ["HOME"])
            self.assertEqual(
                captured["schema"].parent.parent, root.resolve() / "worker-launches"
            )
            self.assertEqual(captured["result"].parent.parent, root / "private-tmp")
            self.assertEqual(
                captured["launch_files"],
                {"permissions.config.toml", "result-schema.json"},
            )
            self.assertEqual(
                cleaned,
                [captured["result"].parent, captured["schema"].parent],
            )

    def test_isolation_overrides_disable_only_configured_servers(self):
        from codex_seats.worker_config import _worker_isolation_overrides

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            # No config / empty config: no per-server overrides at all.
            self.assertFalse(
                [o for o in _worker_isolation_overrides(home) if "mcp_servers" in o]
            )
            # A stale managed_pool block IS disabled — via the config-driven loop.
            (home / "config.toml").write_text(
                '[mcp_servers.managed_pool]\ncommand = "gone"\nrequired = true\n'
            )
            overrides = _worker_isolation_overrides(home)
            self.assertIn("mcp_servers.managed_pool.enabled=false", overrides)
            self.assertIn("mcp_servers.managed_pool.required=false", overrides)

    def test_managed_codex_rejects_full_access_before_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_codex(Path(directory), registry_settings(), {
                "prompt": "unsafe", "sandbox": "danger-full-access",
            })
        self.assertEqual(result.status, "failed")
        self.assertIn("prohibit full-access", result.message)

    def test_boundary_discovery_failure_releases_claim_without_launch(self):
        from codex_seats.write_boundary import GitMetadataError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "seat3"
            home.mkdir()
            (home / "auth.json").write_text("{}")
            settings = write_settings(root, registry_settings())
            with Ledger(root) as data:
                _set_codex_capacity(data["seats"]["seat3"])
            with (
                mock.patch(
                    "codex_seats.provider_codex.boundary_plan",
                    side_effect=GitMetadataError("synthetic discovery failure"),
                ) as boundary,
                mock.patch("codex_seats.workers._execute") as execute,
            ):
                result = run_codex(root, settings, {
                    "prompt": "fixture", "kind": "mechanical", "seat": "seat3",
                    "no_queue": True, "no_probe": True, "cd": str(root),
                })
            boundary.assert_called_once()
            execute.assert_not_called()
            self.assertEqual(result.status, "failed")
            with Ledger(root) as data:
                self.assertIsNone(data["seats"]["seat3"]["in_flight"])


class WorkerMcpProvisioningTests(unittest.TestCase):
    def _repo_with_catalog(self, servers):
        import json
        directory = tempfile.mkdtemp()
        repo = Path(directory)
        (repo / ".seats").mkdir()
        (repo / ".seats" / "worker-mcp.json").write_text(json.dumps({"servers": servers}))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return repo

    def test_granted_server_is_enabled_with_approve_mode(self):
        from codex_seats.workers import worker_mcp_overrides
        repo = self._repo_with_catalog({"visual": {"command": "py", "args": ["s.py"]}})
        ov = worker_mcp_overrides(["visual"], repo)
        self.assertIn("mcp_servers.visual.enabled=true", ov)
        self.assertTrue(any('default_tools_approval_mode="approve"' in o for o in ov))
        self.assertTrue(any(o.startswith("mcp_servers.visual.command=") for o in ov))

    def test_unlisted_grant_fails_closed_and_empty_grant_is_empty(self):
        from codex_seats.workers import worker_mcp_overrides
        repo = self._repo_with_catalog({"visual": {"command": "py"}})
        with self.assertRaisesRegex(ValueError, "not in the repository catalog"):
            worker_mcp_overrides(["not-in-catalog"], repo)
        self.assertEqual(worker_mcp_overrides([], repo), [])

    def test_missing_catalog_grant_fails_closed(self):
        from codex_seats.workers import worker_mcp_overrides
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "catalog is unavailable"):
                worker_mcp_overrides(["visual"], directory)

    def test_malformed_catalog_and_secret_env_fail_closed(self):
        from codex_seats.workers import worker_mcp_overrides

        malformed = self._repo_with_catalog({"visual": {"command": "py", "args": [1]}})
        with self.assertRaisesRegex(ValueError, "args must be strings"):
            worker_mcp_overrides(["visual"], malformed)

        secret = self._repo_with_catalog(
            {"visual": {"command": "py", "env": {"OPENAI_API_KEY": "nope"}}}
        )
        with self.assertRaisesRegex(ValueError, "secret-bearing env keys"):
            worker_mcp_overrides(["visual"], secret)

    def test_catalog_symlink_and_symlinked_parent_fail_closed(self):
        from codex_seats.workers import worker_mcp_overrides

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "worker-mcp.json").write_text(
                json.dumps({"servers": {"visual": {"command": "outside"}}})
            )

            parent_link_repo = root / "parent-link-repo"
            parent_link_repo.mkdir()
            (parent_link_repo / ".seats").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "config directory"):
                worker_mcp_overrides(["visual"], parent_link_repo)

            file_link_repo = root / "file-link-repo"
            (file_link_repo / ".seats").mkdir(parents=True)
            (file_link_repo / ".seats" / "worker-mcp.json").symlink_to(
                outside / "worker-mcp.json"
            )
            with self.assertRaisesRegex(ValueError, "non-symlink file"):
                worker_mcp_overrides(["visual"], file_link_repo)

    def test_catalog_fifo_is_rejected_without_blocking(self):
        from codex_seats.workers import worker_mcp_overrides

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            config = repo / ".seats"
            config.mkdir()
            os.mkfifo(config / "worker-mcp.json")
            with self.assertRaisesRegex(ValueError, "regular file"):
                worker_mcp_overrides(["visual"], repo)

    def test_catalog_read_is_size_bounded_and_refuses_concurrent_change(self):
        from codex_seats import worker_config
        from codex_seats.workers import worker_mcp_overrides

        oversized = self._repo_with_catalog(
            {"visual": {"command": "x" * worker_config.MAX_WORKER_MCP_CATALOG_BYTES}}
        )
        with self.assertRaisesRegex(ValueError, "size limit"):
            worker_mcp_overrides(["visual"], oversized)

        repo = self._repo_with_catalog({"visual": {"command": "trusted-command"}})
        catalog = repo / ".seats" / "worker-mcp.json"
        original_read = os.read
        swapped = False

        def swap_then_read(descriptor, count):
            nonlocal swapped
            if not swapped:
                swapped = True
                catalog.rename(catalog.with_suffix(".original"))
                catalog.write_text(
                    json.dumps({"servers": {"visual": {"command": "evil-command"}}})
                )
            return original_read(descriptor, count)

        with (
            mock.patch("codex_seats.worker_config.os.read", side_effect=swap_then_read),
            self.assertRaisesRegex(ValueError, "changed while it was read"),
        ):
            worker_mcp_overrides(["visual"], repo)

        growing_repo = self._repo_with_catalog(
            {"visual": {"command": "trusted-command"}}
        )
        growing_catalog = growing_repo / ".seats" / "worker-mcp.json"
        grew = False

        def grow_then_read(descriptor, count):
            nonlocal grew
            if not grew:
                grew = True
                with growing_catalog.open("a") as handle:
                    handle.write("x" * (worker_config.MAX_WORKER_MCP_CATALOG_BYTES + 1))
            return original_read(descriptor, count)

        with (
            mock.patch("codex_seats.worker_config.os.read", side_effect=grow_then_read),
            self.assertRaisesRegex(ValueError, "size limit"),
        ):
            worker_mcp_overrides(["visual"], growing_repo)


class EffortRoutingTests(unittest.TestCase):
    """The resolved effort must reach the CLI, be clamped to the backend's
    ceiling, and match what the capacity-learning key records."""

    def _run_claude_capture(self, request, fake_execute):
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, settings)
            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/claude"),
                mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                mock.patch("codex_seats.workers._execute", side_effect=fake_execute),
            ):
                result = run_claude(root, settings, {"_capacity_checked": True, **request})
            with Ledger(root) as data:
                jobs = list(data["jobs"])
                provider = json.loads(json.dumps(data["providers"]["claude"]))
                learning = json.loads(json.dumps(data.get("capacity_learning", {})))
        return result, jobs, provider, learning

    @staticmethod
    def _success_payload():
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "structured_output": {
                "status": "completed",
                "summary": "done",
                "changed_files": [],
                "test_evidence": ["fixture"],
                "warnings": [],
                "needs_controller": None,
            },
        }

    def test_claude_command_receives_kind_effort_default(self):
        # Regression: the Claude head must honor execution.effort_by_kind for the
        # actual --effort flag, not just the telemetry key (review -> medium).
        commands = []

        def fake_execute(command, output, *_args, **_kwargs):
            commands.append(command)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(self._success_payload()))
            return "completed", "{}", 0, 1

        result, jobs, _, _ = self._run_claude_capture(
            {"prompt": "review the diff", "kind": "review"}, fake_execute
        )
        self.assertEqual(result.status, "completed")
        command = commands[0]
        self.assertEqual(command[command.index("--effort") + 1], "medium")
        self.assertEqual(jobs[-1]["effort"], "medium")
        self.assertIsNone(jobs[-1]["effort_downgraded_from"])

    def test_claude_ultra_effort_is_clamped_to_max(self):
        # The claude CLI has no "ultra" — unclamped it silently runs at default.
        commands = []

        def fake_execute(command, output, *_args, **_kwargs):
            commands.append(command)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(self._success_payload()))
            return "completed", "{}", 0, 1

        result, jobs, _, learning = self._run_claude_capture(
            {"prompt": "review the diff", "kind": "review", "effort": "ultra"}, fake_execute
        )
        self.assertEqual(result.status, "completed")
        command = commands[0]
        self.assertEqual(command[command.index("--effort") + 1], "max")
        self.assertEqual(jobs[-1]["effort"], "max")
        self.assertEqual(jobs[-1]["effort_downgraded_from"], "ultra")
        self.assertFalse(any("|ultra" in key for key in learning))

    def _run_codex_capture(self, settings, request):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings["pool"]["seats"]["seat1"]["home"] = str(root / "seat1")
            write_settings(root, settings)
            for seat_id in ["seat1", "seat2", "seat3", "seat4", "seat5"]:
                home = root / seat_id
                home.mkdir()
                (home / "auth.json").write_text("{}")
                (home / "config.toml").write_text("")
            with Ledger(root) as data:
                for seat in data["seats"].values():
                    _set_codex_capacity(seat)
            captured = {}

            def fake_execute(command, *_args, **_kwargs):
                captured["command"] = command
                return "completed", "", 0, 1

            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"),
                mock.patch("codex_seats.workers.sync_codex_config", return_value=False),
                mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                mock.patch("codex_seats.workers._execute", side_effect=fake_execute),
            ):
                result = run_codex(
                    root,
                    settings,
                    {
                        "seat": 3,
                        "no_probe": True,
                        "cd": str(root),
                        "write_scope": ["src/**"],
                        **request,
                    },
                )
            with Ledger(root) as data:
                jobs = list(data["jobs"])
                learning = json.loads(json.dumps(data.get("capacity_learning", {})))
        return result, captured["command"], jobs, learning

    def test_codex_ultra_clamped_without_capability_and_passed_with_it(self):
        # Without enrollment the Codex API 400s on "ultra" — clamp to max.
        result, command, jobs, _ = self._run_codex_capture(
            registry_settings(), {"prompt": "rename this symbol", "kind": "mechanical", "effort": "ultra"}
        )
        self.assertEqual(result.status, "completed")
        self.assertIn("model_reasoning_effort=max", command)
        self.assertEqual(jobs[-1]["effort"], "max")
        self.assertEqual(jobs[-1]["effort_downgraded_from"], "ultra")

        enrolled = registry_settings()
        enrolled["providers"]["codex"]["supports_ultra"] = True
        result, command, jobs, _ = self._run_codex_capture(
            enrolled, {"prompt": "rename this symbol", "kind": "mechanical", "effort": "ultra"}
        )
        self.assertEqual(result.status, "completed")
        self.assertIn("model_reasoning_effort=ultra", command)
        self.assertIsNone(jobs[-1]["effort_downgraded_from"])

    def test_learning_key_uses_default_sentinel_when_effort_absent(self):
        settings = registry_settings()
        settings["execution"]["effort_by_kind"] = {}
        _, command, _, learning = self._run_codex_capture(
            settings, {"prompt": "rename this symbol", "kind": "mechanical"}
        )
        self.assertFalse(any("model_reasoning_effort" in part for part in command))
        self.assertTrue(any(key.endswith("|default") for key in learning))
        self.assertFalse(any("|None" in key for key in learning))
        self.assertTrue(all(key.startswith("codex|windows:") for key in learning))
        self.assertFalse(
            any("|monthly|" in key or "|rolling|" in key for key in learning)
        )

    def test_clamp_effort_table(self):
        from codex_seats.efforts import clamp_effort

        self.assertEqual(clamp_effort("ultra", supports_ultra=False), ("max", "ultra"))
        self.assertEqual(clamp_effort("ultra", supports_ultra=True), ("ultra", None))
        self.assertEqual(clamp_effort("high", supports_ultra=False), ("high", None))
        self.assertEqual(clamp_effort(None, supports_ultra=False), (None, None))


class RefusalFallbackTests(unittest.TestCase):
    """A safety-classifier refusal is model-specific (Fable's cyber classifiers on
    security work): it must advance the fable->opus->sonnet chain without capping
    the model; every other unclassified failure must NOT advance the chain."""

    def _run_security_big(self, fake_execute):
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, settings)
            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/claude"),
                mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                mock.patch("codex_seats.workers._execute", side_effect=fake_execute),
            ):
                result = run_claude(
                    root,
                    settings,
                    {
                        "prompt": "audit the boundary",
                        "kind": "security",
                        "size": "big",
                        "_capacity_checked": True,
                    },
                )
            with Ledger(root) as data:
                jobs = list(data["jobs"])
                provider = json.loads(json.dumps(data["providers"]["claude"]))
        return result, jobs, provider

    def test_refusal_falls_through_to_next_model_without_cap(self):
        commands = []

        def fake_execute(command, output, *_args, **_kwargs):
            commands.append(command)
            output.parent.mkdir(parents=True, exist_ok=True)
            model = command[command.index("--model") + 1]
            if "fable" in model:
                payload = {
                    "type": "result",
                    "subtype": "refusal",
                    "is_error": True,
                    "result": (
                        "Claude Code is unable to respond to this request, "
                        "which appears to violate our Usage Policy."
                    ),
                }
                output.write_text(json.dumps(payload))
                return "completed", json.dumps(payload), 0, 1
            payload = EffortRoutingTests._success_payload()
            output.write_text(json.dumps(payload))
            return "completed", json.dumps(payload), 0, 1

        result, jobs, provider = self._run_security_big(fake_execute)
        self.assertEqual(result.status, "completed")
        self.assertEqual(
            [command[command.index("--model") + 1] for command in commands],
            ["claude-fable-5-1", "claude-opus-5-5"],
        )
        fable_state = provider["models"].get("claude-fable-5-1", {})
        self.assertNotEqual(fable_state.get("status"), "capped")
        self.assertIn("last_refusal_at", fable_state)
        self.assertEqual(jobs[-1]["refused_models"], ["claude-fable-5-1"])

    def test_generic_error_does_not_advance_chain(self):
        commands = []

        def fake_execute(command, output, *_args, **_kwargs):
            commands.append(command)
            output.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "TypeError: boom",
            }
            output.write_text(json.dumps(payload))
            return "failed", json.dumps(payload), 1, 1

        result, jobs, _ = self._run_security_big(fake_execute)
        self.assertNotEqual(result.status, "completed")
        self.assertEqual(len(commands), 1)
        self.assertEqual(jobs[-1]["refused_models"], [])

    def test_refusal_wording_in_task_error_does_not_advance(self):
        # A failed run whose error text merely mentions the word must not trip
        # the conservative signature (no bare \brefusal\b in REFUSAL_ERR).
        commands = []

        def fake_execute(command, output, *_args, **_kwargs):
            commands.append(command)
            output.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "the reviewer noted a refusal pattern in the corpus",
            }
            output.write_text(json.dumps(payload))
            return "failed", json.dumps(payload), 1, 1

        result, jobs, _ = self._run_security_big(fake_execute)
        self.assertNotEqual(result.status, "completed")
        self.assertEqual(len(commands), 1)
        self.assertEqual(jobs[-1]["refused_models"], [])


if __name__ == "__main__":
    unittest.main()
