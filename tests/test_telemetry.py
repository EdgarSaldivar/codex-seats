import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_seats.state import Ledger, iso, new_seat
from registry_fixture import registry_settings, write_settings
from codex_seats.telemetry import (
    apply_usage_limits,
    limits_from_event,
    read_rollout_telemetry,
    record_account_observation_failure,
    record_usage_cycles,
    refresh_passive_usage,
)


def token_event(tokens, cached, used, minutes, reset, *, timestamp="2026-07-11T07:00:00Z"):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "total_tokens": tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": 10,
                },
                "last_token_usage": {"total_tokens": 500},
                "model_context_window": 1000,
            },
            "rate_limits": {
                "primary": {
                    "used_percent": used,
                    "window_minutes": minutes,
                    "resets_at": reset,
                }
            },
        },
    }


def turn_context(*, mode="plan", effort="max"):
    return {
        "timestamp": "2026-07-11T07:00:01Z",
        "type": "turn_context",
        "payload": {
            "model": "gpt-5.6-sol",
            "effort": effort,
            "approval_policy": "on-request",
            "approvals_reviewer": "auto_review",
            "collaboration_mode": {
                "mode": mode,
                "settings": {"model": "gpt-5.6-sol", "reasoning_effort": effort},
            },
        },
    }


class PassiveTelemetryTests(unittest.TestCase):
    def test_empty_idle_seat_discovery_is_throttled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            with Ledger(root) as data, mock.patch(
                "codex_seats.telemetry._rollouts", return_value=[]
            ) as discover:
                refresh_passive_usage(data, root, settings, ["seat1"], at=100)
                refresh_passive_usage(data, root, settings, ["seat1"], at=102)
                self.assertEqual(discover.call_count, 1)

    def test_idle_seat_periodically_discovers_a_newer_interactive_rollout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings(primary_home=root / "primary"))
            home = root / "primary"
            sessions = home / "sessions" / "2026" / "07" / "16"
            sessions.mkdir(parents=True)
            current = time.time()
            reset = current + 7200
            old = sessions / "rollout-old.jsonl"
            old.write_text(json.dumps(token_event(100, 50, 70, 43800, reset, timestamp=iso(current - 100))) + "\n")
            os.utime(old, (current - 100, current - 100))
            with Ledger(root) as data:
                refresh_passive_usage(data, root, settings, ["seat1"], force_discover=True, at=current)
                self.assertEqual(data["seats"]["seat1"]["pct_left"], 30)

            newer = sessions / "rollout-new.jsonl"
            newer.write_text(json.dumps(token_event(200, 100, 10, 43800, reset, timestamp=iso(current + 1))) + "\n")
            os.utime(newer, (current + 1, current + 1))
            with Ledger(root) as data:
                changed = refresh_passive_usage(
                    data,
                    root,
                    settings,
                    ["seat1"],
                    at=current + settings["pool"]["telemetry_scan_seconds"] + 1,
                )
                self.assertEqual(changed, ["seat1"])
                self.assertEqual(data["seats"]["seat1"]["pct_left"], 90)
                self.assertEqual(data["seats"]["seat1"]["telemetry_file"], str(newer))

    def test_older_passive_event_cannot_replace_newer_account_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings(primary_home=root / "primary"))
            home = root / "primary"
            sessions = home / "sessions"
            sessions.mkdir(parents=True)
            current = time.time()
            rollout = sessions / "rollout-old.jsonl"
            rollout.write_text(
                json.dumps(
                    token_event(
                        100,
                        50,
                        90,
                        43800,
                        current + 7200,
                        timestamp=iso(current - 60),
                    )
                )
                + "\n"
            )
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                seat.update(
                    {
                        "pct_left": 75,
                        "usage_source": "account",
                        "usage_updated_at": iso(current),
                        "probed_at": iso(current),
                    }
                )
                changed = refresh_passive_usage(
                    data, root, settings, ["seat1"], force_discover=True, at=current + 1
                )
                self.assertEqual(changed, [])
                self.assertEqual(seat["pct_left"], 75)
                self.assertEqual(seat["usage_source"], "account")

    def test_new_reader_repairs_legacy_passive_rollback_from_account_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                apply_usage_limits(
                    seat,
                    {"weekly": {"pct": 73, "resets_at": 9999}},
                    {"tokens": 100, "cached_tokens": 80, "output_tokens": 5},
                    200,
                    source="account",
                )

            # Simulate a controller that loaded the pre-ordering-fix code: it
            # overwrites visible values but preserves the unknown snapshot.
            raw = json.loads((root / "seats.json").read_text())
            raw["seats"]["seat1"].update(
                {
                    "last_probe_limits": {"weekly": {"pct": 12, "resets_at": 8888}},
                    "pct_left": 12,
                    "type": "rolling",
                    "probed_at": iso(100),
                    "usage_updated_at": iso(100),
                    "usage_source": "passive",
                    "usage_reset_windows": {},
                    "active_window_observation": {
                        "source": "passive",
                        "complete": False,
                        "observed_at": 100,
                    },
                }
            )
            (root / "seats.json").write_text(json.dumps(raw))

            # Every current ledger consumer gets the repair, even if it never
            # invokes passive telemetry discovery itself.
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                self.assertEqual(seat["pct_left"], 73)
                self.assertEqual(seat["type"], "window")
                self.assertEqual(seat["account_usage_snapshot"]["type"], "window")
                self.assertEqual(seat["usage_source"], "account")
                self.assertEqual(seat["last_probe_limits"]["weekly"]["pct"], 73)
                self.assertEqual(seat["usage_updated_at"], iso(200))
                self.assertEqual(
                    seat["active_window_observation"],
                    seat["account_usage_snapshot"]["active_window_observation"],
                )
                self.assertEqual(
                    seat["usage_reset_windows"],
                    seat["account_usage_snapshot"]["usage_reset_windows"],
                )

            # The repair must PERSIST, not just live in memory: re-read the file
            # directly (the prior gap — the repair ran on every open but was
            # never written back, so disk stayed rolled back forever).
            on_disk = json.loads((root / "seats.json").read_text())
            self.assertEqual(on_disk["seats"]["seat1"]["pct_left"], 73)

    def test_same_second_stale_event_cannot_roll_back_a_newer_read(self):
        from codex_seats.telemetry import _apply_latest

        seat = new_seat()
        apply_usage_limits(
            seat, {"weekly": {"pct": 80, "resets_at": 9999}}, {}, 100.9, source="account"
        )
        self.assertEqual(seat["pct_left"], 80)
        # A stale rollout observed 0.8s EARLIER, same wall-clock second. iso()
        # truncates both to "100", so a string compare would tie and let it win.
        applied = _apply_latest(
            seat,
            {"rate_limits": {"weekly": {"remaining_percentage": 40, "resets_at": 9999}},
             "event_ts": 100.1, "path": "x", "mtime_ns": 1},
            100.1,
        )
        self.assertFalse(applied)
        self.assertEqual(seat["pct_left"], 80)  # newer read stands

    def test_last_probe_limits_does_not_alias_the_caller_limits(self):
        seat = new_seat()
        limits = {"weekly": {"pct": 55, "resets_at": 9999}}
        apply_usage_limits(seat, limits, {}, 100, source="account")
        limits["weekly"]["pct"] = -1  # mutate the caller's dict after the fact
        self.assertEqual(seat["last_probe_limits"]["weekly"]["pct"], 55)

    def test_reads_latest_token_event_and_maps_monthly_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-test.jsonl"
            path.write_text(json.dumps(token_event(1234, 1000, 7, 43800, time.time() + 3600)) + "\n")
            payload = read_rollout_telemetry(path)
            self.assertEqual(payload["info"]["total_token_usage"]["total_tokens"], 1234)
            limits = limits_from_event(payload["rate_limits"])
            self.assertEqual(limits["monthly"]["pct"], 93)

    def test_reads_latest_turn_mode_and_effort_with_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-test.jsonl"
            path.write_text(
                json.dumps(token_event(1234, 1000, 7, 43800, time.time() + 3600))
                + "\n"
                + json.dumps(turn_context())
                + "\n"
            )
            payload = read_rollout_telemetry(path)
            self.assertEqual(payload["session"]["mode"], "plan")
            self.assertEqual(payload["session"]["effort"], "max")
            self.assertEqual(payload["session"]["approvals_reviewer"], "auto_review")

    def test_active_claim_sums_root_and_subagent_and_updates_exact_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings(primary_home=root / "primary"))
            home = root / "primary"
            sessions = home / "sessions" / "2026" / "07" / "11"
            sessions.mkdir(parents=True)
            (home / "auth.json").write_text("{}")
            reset = time.time() + 3600
            for index, tokens in enumerate((1000, 2500), 1):
                path = sessions / f"rollout-test-{index}.jsonl"
                path.write_text(json.dumps(token_event(tokens, tokens - 100, 7, 43800, reset)) + "\n")
            with Ledger(root) as data:
                data["seats"]["seat1"].update({"status": "ok", "pct_left": 100})
                data["seats"]["seat1"]["in_flight"] = {
                    "job_id": "control-test",
                    "dispatcher_pid": os.getpid(),
                    "started_at": "2026-07-11T06:59:30Z",
                    "kind": "control",
                }
                changed = refresh_passive_usage(
                    data, root, settings, ["seat1"], force_discover=True
                )
                seat = data["seats"]["seat1"]
                self.assertEqual(changed, ["seat1"])
                self.assertEqual(seat["pct_left"], 93)
                self.assertEqual(seat["usage_source"], "passive")
                self.assertEqual(seat["in_flight"]["tokens"], 3500)
                self.assertEqual(seat["in_flight"]["output_tokens"], 20)
                self.assertEqual(seat["in_flight"]["rollout_count"], 2)
                self.assertEqual(seat["in_flight"]["context_pct"], 50)

    def test_expired_short_window_is_not_a_routing_constraint(self):
        limits = limits_from_event(
            {
                "primary": {
                    "used_percent": 95,
                    "window_minutes": 300,
                    "resets_at": 100,
                },
                "secondary": {
                    "used_percent": 20,
                    "window_minutes": 10080,
                    "resets_at": 1000,
                },
            },
            at=200,
        )
        self.assertNotIn("5h", limits)
        self.assertEqual(limits["weekly"]["pct"], 80)

    def test_passive_partial_snapshot_retains_other_future_active_window_until_expiry(self):
        seat = new_seat()
        entry = {"tokens": 0, "cached_tokens": 0, "output_tokens": 0}
        apply_usage_limits(
            seat,
            {
                "5h": {"pct": 80, "resets_at": 500},
                "weekly": {"pct": 70, "resets_at": 1000},
            },
            entry,
            100,
            source="passive",
        )
        apply_usage_limits(
            seat,
            {"weekly": {"pct": 90, "resets_at": 1100}},
            entry,
            200,
            source="passive",
        )
        self.assertEqual(set(seat["last_probe_limits"]), {"5h", "weekly"})
        self.assertEqual(seat["last_probe_limits"]["5h"]["pct"], 80)
        self.assertEqual(seat["usage_reset_windows"]["5h"]["resets_at"], 500)
        self.assertEqual(seat["usage_reset_windows"]["weekly"]["resets_at"], 1100)

        apply_usage_limits(
            seat,
            {"weekly": {"pct": 85, "resets_at": 1200}},
            entry,
            600,
            source="passive",
        )
        self.assertNotIn("5h", seat["usage_reset_windows"])
        self.assertNotIn("5h", seat["last_probe_limits"])

    def test_older_passive_apply_cannot_overwrite_authoritative_metadata(self):
        seat = new_seat()
        apply_usage_limits(
            seat,
            {"weekly": {"pct": 75, "resets_at": 9000}},
            {},
            200.9,
            source="account",
            observation_complete=True,
        )
        expected_observation = seat["active_window_observation"].copy()

        applied = apply_usage_limits(
            seat,
            {"weekly": {"pct": 10, "resets_at": 8000}},
            {},
            200.1,
            source="passive",
        )

        self.assertFalse(applied)
        self.assertEqual(seat["last_probe_limits"]["weekly"]["pct"], 75)
        self.assertEqual(seat["active_window_observation"], expected_observation)

    def test_repeated_account_failures_preserve_initial_stale_since(self):
        seat = new_seat()

        record_account_observation_failure(seat, 200, "first failure")
        record_account_observation_failure(seat, 300, "second failure")

        observation = seat["account_window_observation"]
        self.assertEqual(observation["stale_since"], iso(200))
        self.assertEqual(observation["last_failure_at"], iso(300))
        self.assertEqual(observation["last_error"], "second failure")

    def test_observed_cycles_ignore_moving_target_until_reset_is_confirmed(self):
        seat = new_seat()
        entry = {"tokens": 1000, "cached_tokens": 800, "output_tokens": 50}
        record_usage_cycles(
            seat,
            {"weekly": {"pct": 60, "resets_at": 2000}},
            entry,
            1000,
        )
        record_usage_cycles(
            seat,
            {"weekly": {"pct": 58, "resets_at": 2100}},
            {"tokens": 2000, "cached_tokens": 1600, "output_tokens": 80},
            1100,
        )
        cycle = seat["usage_cycles"]["weekly"]
        self.assertEqual(cycle["observed_resets"], 0)
        self.assertEqual(cycle["consumed_pct"], 2)
        record_usage_cycles(
            seat,
            {"weekly": {"pct": 95, "resets_at": 3000}},
            {"tokens": 10, "cached_tokens": 0, "output_tokens": 2},
            1200,
        )
        cycle = seat["usage_cycles"]["weekly"]
        self.assertEqual(cycle["observed_resets"], 1)
        self.assertEqual(cycle["starting_pct"], 95)
        self.assertEqual(cycle["previous_cycles"][0]["unused_pct"], 58)


if __name__ == "__main__":
    unittest.main()
