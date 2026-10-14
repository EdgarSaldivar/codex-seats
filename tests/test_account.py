import unittest
import tempfile
import time
from pathlib import Path
from unittest import mock

from codex_seats.account import (
    AccountReadError,
    _app_server_call,
    apply_manager_identity_snapshot,
    apply_account_snapshot,
    limits_from_account,
    manager_identity_report,
    read_account_rate_limits,
    refresh_account_usage,
)
from codex_seats.capacity import capacity_task_fits
from codex_seats.state import Ledger, new_seat
from registry_fixture import registry_settings, write_settings


def account_payload():
    return {
        "rateLimits": {
            "limitId": "codex",
            "primary": {
                "usedPercent": 25,
                "windowDurationMins": 300,
                "resetsAt": 5000,
            },
            "secondary": {
                "usedPercent": 40,
                "windowDurationMins": 10080,
                "resetsAt": 9000,
            },
        },
        "rateLimitsByLimitId": None,
    }


class AccountTelemetryTests(unittest.TestCase):
    def test_optional_identity_broken_pipe_preserves_usage(self):
        process = mock.Mock(stdout=None)
        usage = account_payload()

        def send(_process, request):
            if request.get("method") == "account/read":
                raise BrokenPipeError("provider exited")

        with (
            mock.patch("codex_seats.account.subprocess.Popen", return_value=process),
            mock.patch("codex_seats.account._send", side_effect=send),
            mock.patch("codex_seats.account._wait_response", side_effect=[
                {"id": 1, "result": {}}, {"id": 2, "result": usage},
            ]),
            mock.patch("codex_seats.account._stop_process"),
        ):
            payload = _app_server_call(
                "/fake/codex", Path("/home"), Path("/scratch"),
                "account/rateLimits/read", optional_account_identity=True,
            )
        self.assertEqual(payload["rateLimits"], usage["rateLimits"])
        self.assertFalse(payload["_codex_seats_manager_identity"]["available"])

    def test_optional_identity_uses_same_app_server_and_disables_token_refresh(self):
        process = mock.Mock(stdout=None)
        responses = [
            {"id": 1, "result": {}},
            {"id": 2, "result": account_payload()},
            {"id": 3, "result": {
                "requiresOpenaiAuth": True,
                "account": {"type": "chatgpt", "email": "seat@example.test"},
            }},
        ]
        with (
            mock.patch("codex_seats.account.subprocess.Popen", return_value=process) as popen,
            mock.patch("codex_seats.account._send") as send,
            mock.patch("codex_seats.account._wait_response", side_effect=responses),
            mock.patch("codex_seats.account._stop_process"),
        ):
            payload = _app_server_call(
                "/fake/codex", Path("/home"), Path("/scratch"),
                "account/rateLimits/read", optional_account_identity=True,
            )
        popen.assert_called_once()
        identity_request = send.call_args_list[-1].args[1]
        self.assertEqual(identity_request["method"], "account/read")
        self.assertEqual(identity_request["params"], {"refreshToken": False})
        self.assertTrue(payload["_codex_seats_manager_identity"]["available"])

    def test_rate_limit_read_can_request_provider_owned_manager_identity(self):
        with mock.patch(
            "codex_seats.account._app_server_call", return_value=account_payload()
        ) as call:
            read_account_rate_limits(
                "/fake/codex", Path("/seat-home"), Path("/scratch"),
                include_manager_identity=True,
            )
        self.assertEqual(call.call_args.args[3], "account/rateLimits/read")
        self.assertTrue(call.call_args.kwargs["optional_account_identity"])

    def test_provider_identity_cache_keeps_plan_not_workspace_and_is_auth_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            home = root / "seat1"
            home.mkdir()
            auth = home / "auth.json"
            auth.write_text("first login")
            payload = account_payload()
            payload.update({
                "accountId": "acct-provider-1",
                "_codex_seats_manager_identity": {
                    "available": True,
                    "account": {
                        "requiresOpenaiAuth": True,
                        "account": {
                            "type": "chatgpt", "email": "seat@example.test",
                            "planType": "business",
                            "workspace": {
                                "name": "Example Workspace", "id": "workspace-1",
                                "secret": "not-allowlisted",
                            },
                        },
                        "unexpected": "not-allowlisted",
                    },
                },
            })
            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"),
                mock.patch("codex_seats.account.read_account_rate_limits", return_value=payload),
            ):
                refresh_account_usage(root, registry_settings(), ["seat1"])
            report = manager_identity_report(root, registry_settings(), "seat1")
            self.assertEqual(report["email"], "seat@example.test")
            self.assertEqual(report["observed_plan_type"], "business")
            self.assertEqual(report["account_id"], "acct-provider-1")
            self.assertNotIn("workspace_name", report)
            self.assertNotIn("workspace_id", report)
            with Ledger(root) as data:
                cached = data["seats"]["seat1"]["manager_identity"]
                self.assertEqual(cached["observed_plan_type"], "business")
                self.assertNotIn("workspace", repr(cached))
                self.assertNotIn("secret", repr(cached))
                self.assertNotIn("unexpected", repr(cached))
            auth.write_text("second login with different bytes")
            stale = manager_identity_report(root, registry_settings(), "seat1")
            self.assertEqual(stale["status"], "stale")
            self.assertIsNone(stale["email"])
            self.assertIsNone(stale["observed_plan_type"])
            self.assertIn("login changed", stale["reason"])

    def test_transient_identity_failure_marks_same_login_stale(self):
        seat = new_seat()
        fingerprint = {
            "device": 1, "inode": 2, "size": 3, "mtime_ns": 4, "ctime_ns": 5,
        }
        good = account_payload()
        good["_codex_seats_manager_identity"] = {
            "available": True,
            "account": {"account": {
                "type": "chatgpt", "email": "last-good@example.test", "planType": "plus",
            }},
        }
        self.assertTrue(apply_manager_identity_snapshot(seat, good, 1000, fingerprint))
        unavailable = account_payload()
        unavailable["_codex_seats_manager_identity"] = {"available": False}
        self.assertTrue(apply_manager_identity_snapshot(seat, unavailable, 1100, fingerprint))
        self.assertEqual(seat["manager_identity"]["status"], "stale")
        self.assertEqual(seat["manager_identity"]["email"], "last-good@example.test")
        self.assertEqual(seat["manager_identity"]["observed_plan_type"], "plus")

    def test_broad_plan_types_remain_literal_and_old_workspace_cache_is_removed(self):
        seat = new_seat()
        fingerprint = {
            "device": 1, "inode": 2, "size": 3, "mtime_ns": 4, "ctime_ns": 5,
        }
        seat["manager_identity"].update({
            "workspace_name": "old unsupported value",
            "workspace_id": "old-workspace-id",
        })
        payload = account_payload()
        payload["rateLimits"]["planType"] = "business"
        payload["_codex_seats_manager_identity"] = {
            "available": True,
            "account": {"account": {
                "type": "chatgpt", "email": "broad@example.test",
                "planType": "pro",
            }},
        }
        self.assertTrue(apply_manager_identity_snapshot(seat, payload, 1000, fingerprint))
        apply_account_snapshot(seat, payload, 1000)
        identity = seat["manager_identity"]
        self.assertEqual(identity["observed_plan_type"], "pro")
        self.assertEqual(seat["account_usage_plan_type"], "business")
        self.assertNotIn("workspace_name", identity)
        self.assertNotIn("workspace_id", identity)

        limits = limits_from_account(payload, at=1000)
        self.assertEqual(limits["5h"]["plan_type"], "business")
        self.assertEqual(limits["weekly"]["plan_type"], "business")

    def test_ledger_migration_removes_old_cached_workspace_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            with Ledger(root) as data:
                identity = data["seats"]["seat1"]["manager_identity"]
                identity["workspace_name"] = "retired-name"
                identity["workspace_id"] = "retired-id"
                identity.pop("observed_plan_type", None)
            with Ledger(root) as data:
                migrated = data["seats"]["seat1"]["manager_identity"]
                self.assertNotIn("workspace_name", migrated)
                self.assertNotIn("workspace_id", migrated)
                self.assertIsNone(migrated["observed_plan_type"])
            persisted = (root / "seats.json").read_text()
            self.assertNotIn("retired-name", persisted)
            self.assertNotIn("retired-id", persisted)

    def test_workspace_credit_metrics_are_literal_optional_and_never_invented(self):
        seat = new_seat()
        payload = account_payload()
        payload["rateLimits"]["credits"] = {
            "hasCredits": True, "unlimited": False, "balance": "42.50",
        }
        apply_account_snapshot(seat, payload, 1000)
        self.assertEqual(seat["account_usage_credits"], {
            "has_credits": True, "unlimited": False, "balance": "42.50",
        })

        missing = account_payload()
        apply_account_snapshot(seat, missing, 1100)
        self.assertIsNone(seat["account_usage_credits"])

        incomplete = account_payload()
        incomplete["rateLimits"]["credits"] = {}
        apply_account_snapshot(seat, incomplete, 1200)
        self.assertEqual(seat["account_usage_credits"], {
            "has_credits": None, "unlimited": None, "balance": None,
        })

    def test_malformed_long_window_never_admits_partial_fresh_capacity(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            home = root / "seat1"
            home.mkdir()
            (home / "auth.json").touch()
            observed = time.time()
            malformed = account_payload()
            malformed["rateLimits"]["primary"]["resetsAt"] = observed + 3600
            malformed["rateLimits"]["secondary"]["resetsAt"] = observed + 7200
            malformed["rateLimits"]["secondary"]["windowDurationMins"] = "weekly"
            with (
                mock.patch(
                    "codex_seats.workers.resolve_executable", return_value="/fake/codex"
                ),
                mock.patch(
                    "codex_seats.account.read_account_rate_limits",
                    return_value=malformed,
                ),
            ):
                messages = refresh_account_usage(
                    root, registry_settings(), ["seat1"]
                )
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                observation = seat["account_window_observation"]
                self.assertEqual(observation["status"], "failed")
                self.assertIsNone(observation["last_success_observed_at"])
                self.assertIsNone(observation["last_complete_observed_at"])
                self.assertEqual(seat["last_probe_limits"], {})
                fits, _reason = capacity_task_fits(
                    seat,
                    "small",
                    current=observed,
                    stale_seconds=registry_settings()["pool"]["probe_stale_seconds"],
                    settings=registry_settings(),
                )
                self.assertFalse(fits)
            self.assertIn("unavailable", messages[0])

    def test_valid_single_window_and_expired_or_empty_enumerations_are_complete(self):
        observed = 10_000.0
        premium = account_payload()
        premium["rateLimits"].pop("primary")
        premium["rateLimits"]["secondary"]["resetsAt"] = observed + 3600
        premium_seat = new_seat()
        apply_account_snapshot(premium_seat, premium, observed)
        self.assertEqual(set(premium_seat["last_probe_limits"]), {"weekly"})
        self.assertTrue(premium_seat["active_window_observation"]["complete"])

        expired = account_payload()
        expired["rateLimits"]["primary"]["resetsAt"] = observed - 1
        expired["rateLimits"]["secondary"]["resetsAt"] = observed - 1
        expired_seat = new_seat()
        apply_account_snapshot(expired_seat, expired, observed)
        self.assertTrue(expired_seat["active_window_observation"]["complete"])
        self.assertEqual(expired_seat["last_probe_limits"], {})

        empty_seat = new_seat()
        apply_account_snapshot(empty_seat, {"rateLimits": None}, observed)
        self.assertTrue(empty_seat["active_window_observation"]["complete"])

    def test_malformed_refresh_records_failure_and_preserves_last_good(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            (root / "seat1").mkdir()
            (root / "seat1" / "auth.json").write_text("{}")
            observed = time.time()
            good = account_payload()
            good["rateLimits"]["primary"]["resetsAt"] = observed + 3600
            good["rateLimits"]["secondary"]["resetsAt"] = observed + 7200
            with Ledger(root) as data:
                apply_account_snapshot(data["seats"]["seat1"], good, observed)
                expected = data["seats"]["seat1"]["last_probe_limits"].copy()
            malformed = account_payload()
            malformed["rateLimits"]["secondary"]["usedPercent"] = "unknown"
            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"),
                mock.patch(
                    "codex_seats.account.read_account_rate_limits",
                    return_value=malformed,
                ),
            ):
                messages = refresh_account_usage(root, registry_settings(), ["seat1"])
            self.assertIn("seat1 account -> unavailable", messages[0])
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                self.assertEqual(seat["last_probe_limits"], expected)
                self.assertEqual(seat["account_window_observation"]["status"], "failed")
                self.assertEqual(
                    seat["account_window_observation"]["last_complete_observed_at"],
                    observed,
                )

    def test_complete_weekly_only_account_snapshot_prunes_absent_active_window(self):
        seat = new_seat()
        initial = account_payload()
        initial["rateLimits"]["secondary"]["windowDurationMins"] = 43800
        apply_account_snapshot(seat, initial, 1000)
        self.assertEqual(set(seat["last_probe_limits"]), {"5h", "monthly"})
        prior_5h_history = seat["usage_cycles"]["5h"].copy()
        prior_monthly_history = seat["usage_cycles"]["monthly"].copy()

        weekly_only = account_payload()
        weekly_only["rateLimits"].pop("primary")
        apply_account_snapshot(seat, weekly_only, 1200)

        self.assertEqual(set(seat["last_probe_limits"]), {"weekly"})
        self.assertEqual(seat["type"], "window")
        self.assertEqual(set(seat["usage_reset_windows"]), {"weekly"})
        self.assertEqual(seat["usage_cycles"]["5h"], prior_5h_history)
        self.assertEqual(seat["usage_cycles"]["monthly"], prior_monthly_history)
        self.assertTrue(seat["active_window_observation"]["complete"])
        self.assertEqual(
            seat["active_window_observation"]["observed_windows"], ["weekly"]
        )

    def test_explicit_null_enumeration_prunes_but_omitted_collection_is_partial(self):
        seat = new_seat()
        apply_account_snapshot(seat, account_payload(), 1000)

        omitted = {}
        apply_account_snapshot(seat, omitted, 1100)
        self.assertEqual(set(seat["last_probe_limits"]), {"5h", "weekly"})
        self.assertEqual(
            seat["account_window_observation"]["last_success_observed_at"], 1100
        )
        self.assertEqual(seat["account_window_observation"]["status"], "partial")
        self.assertFalse(
            seat["account_window_observation"]["last_attempt_complete"]
        )

        explicit_null = {"rateLimits": None}
        apply_account_snapshot(seat, explicit_null, 1200)
        self.assertEqual(seat["last_probe_limits"], {})
        self.assertEqual(seat["usage_reset_windows"], {})
        self.assertIsNone(seat["pct_left"])
        self.assertTrue(seat["active_window_observation"]["complete"])

    def test_partial_account_failure_retains_last_good_seat_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            for seat_id in ("seat1", "seat2"):
                home = root / seat_id
                home.mkdir()
                (home / "auth.json").write_text("{}")
            with Ledger(root) as data:
                prior = account_payload()
                current = time.time()
                prior["rateLimits"]["primary"]["resetsAt"] = current + 3600
                prior["rateLimits"]["secondary"]["resetsAt"] = current + 7200
                apply_account_snapshot(
                    data["seats"]["seat1"], prior, current
                )
                data["seats"]["seat2"].update({"pct_left": 66})
                expected_limits = data["seats"]["seat1"]["last_probe_limits"].copy()
                expected_active = data["seats"]["seat1"][
                    "active_window_observation"
                ].copy()

            def read(_codex, home, _scratch, **_kwargs):
                if home.name == "seat1":
                    raise AccountReadError("temporary failure")
                current = time.time()
                payload = account_payload()
                payload["rateLimits"]["primary"]["resetsAt"] = current + 3600
                payload["rateLimits"]["secondary"]["resetsAt"] = current + 7200
                return payload

            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"),
                mock.patch("codex_seats.account.read_account_rate_limits", side_effect=read),
            ):
                messages = refresh_account_usage(root, registry_settings(), ["seat1", "seat2"])
            with Ledger(root) as data:
                failed = data["seats"]["seat1"]
                self.assertEqual(failed["last_probe_limits"], expected_limits)
                self.assertEqual(failed["active_window_observation"], expected_active)
                self.assertEqual(failed["usage_source"], "account")
                self.assertEqual(
                    failed["account_window_observation"]["status"], "failed"
                )
                self.assertEqual(
                    failed["account_window_observation"]["last_error"],
                    "temporary failure",
                )
                self.assertIsNotNone(
                    failed["account_window_observation"]["stale_since"]
                )
                self.assertEqual(data["seats"]["seat2"]["pct_left"], 60)
            self.assertIn("seat1 account -> unavailable", messages[0])
            self.assertIn("seat2 account -> ok", messages[1])

    def test_busy_sqlite_runtime_retries_with_private_account_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            home = root / "seat1"
            home.mkdir()
            (home / "auth.json").write_text("{}")
            current = time.time()
            payload = account_payload()
            payload["rateLimits"]["primary"]["resetsAt"] = current + 3600
            payload["rateLimits"]["secondary"]["resetsAt"] = current + 7200
            homes = []

            def read(_codex, candidate, _scratch, **_kwargs):
                homes.append(candidate)
                if candidate == home:
                    raise AccountReadError("failed to initialize sqlite state runtime")
                self.assertEqual((candidate / "auth.json").read_text(), "{}")
                return payload

            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"),
                mock.patch("codex_seats.account.read_account_rate_limits", side_effect=read),
            ):
                messages = refresh_account_usage(root, registry_settings(), ["seat1"])
            self.assertEqual(len(homes), 2)
            self.assertNotEqual(homes[1], home)
            self.assertFalse(homes[1].exists())
            self.assertIn("seat1 account -> ok", messages[0])

    def test_unauthorized_account_probe_quarantines_existing_auth_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            home = root / "seat1"
            home.mkdir()
            (home / "auth.json").write_text("{}")
            with Ledger(root) as data:
                data["seats"]["seat1"]["status"] = "ok"
            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"),
                mock.patch(
                    "codex_seats.account.read_account_rate_limits",
                    side_effect=AccountReadError("401 Unauthorized: invalid_grant"),
                ),
            ):
                refresh_account_usage(root, registry_settings(), ["seat1"])
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                self.assertEqual(seat["status"], "needs-login")
                self.assertEqual(seat["auth_failure"], "invalid grant")

    def test_successful_authoritative_account_probe_recovers_quarantined_seat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            home = root / "seat1"
            home.mkdir()
            (home / "auth.json").write_text("{}")
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                seat["status"] = "needs-login"
                seat["auth_quarantined_at"] = "2026-07-12T00:00:00Z"
                seat["auth_failure"] = "refresh token revoked"
            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"),
                mock.patch("codex_seats.account.read_account_rate_limits", return_value=account_payload()),
            ):
                refresh_account_usage(root, registry_settings(), ["seat1"])
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                self.assertEqual(seat["status"], "ok")
                self.assertIsNone(seat["auth_quarantined_at"])
                self.assertIsNone(seat["auth_failure"])

    def test_successful_account_probe_does_not_clear_capacity_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            home = root / "seat1"
            home.mkdir()
            (home / "auth.json").write_text("{}")
            with Ledger(root) as data:
                seat = data["seats"]["seat1"]
                seat["status"] = "capped"
                seat["status_until"] = time.time() + 3600
            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"),
                mock.patch("codex_seats.account.read_account_rate_limits", return_value=account_payload()),
            ):
                refresh_account_usage(root, registry_settings(), ["seat1"])
            with Ledger(root) as data:
                self.assertEqual(data["seats"]["seat1"]["status"], "capped")

    def test_maps_structured_account_windows(self):
        limits = limits_from_account(account_payload(), at=1000)
        self.assertEqual(limits["5h"]["pct"], 75)
        self.assertEqual(limits["weekly"]["pct"], 60)
        self.assertEqual(limits["5h"]["resets_at"], 5000)

    def test_account_snapshot_updates_windows(self):
        seat = new_seat()
        seat["status"] = "ok"
        apply_account_snapshot(seat, account_payload(), 1000)
        self.assertEqual(seat["pct_left"], 60)
        self.assertEqual(seat["usage_source"], "account")


if __name__ == "__main__":
    unittest.main()
