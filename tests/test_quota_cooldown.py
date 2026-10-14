from __future__ import annotations

import json
import math
import os
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_seats.cli import cmd_provider_set
from codex_seats.provider_claude import run_claude
from codex_seats.quota_cooldown import (
    RESET_GRACE_SECONDS,
    bounded_cooldown_until,
    classify_quota_window,
    quota_cooldown_provenance,
    quota_cooldown_recovered,
    quota_evidence_from_snapshot,
)
from codex_seats.state import (
    Ledger,
    load_claude_rate_limits,
    refresh_transients,
)
from registry_fixture import registry_settings, write_settings


SESSION_LIMIT = "You've reached your session limit · resets 10:30pm (America/Los_Angeles)"
WEEKLY_LIMIT = "You've reached your weekly limit · resets Tue 09:00"
AMBIGUOUS_LIMIT = "Claude usage limit reached · resets 22:30"
STALE = 6 * 3600


def provenance(
    created_at: float, resets_at: float | None, observed_at: float | None = ...
) -> dict:
    return {
        "kind": "provider-quota",
        "created_at": created_at,
        "window": "five_hour",
        "window_resets_at": resets_at,
        "window_observed_at": created_at if observed_at is ... else observed_at,
    }


def windows(
    current: float,
    *,
    five_hour_used: float = 0.0,
    five_hour_resets: float | None = None,
    five_hour_observed: float | None = None,
    seven_day_used: float = 25.0,
    seven_day_resets: float | None = None,
    seven_day_observed: float | None = None,
) -> dict:
    return {
        "five_hour": {
            "used_percentage": five_hour_used,
            "remaining_percentage": 100 - five_hour_used,
            "resets_at": current + 17000 if five_hour_resets is None else five_hour_resets,
            "observed_at": current - 5 if five_hour_observed is None else five_hour_observed,
        },
        "seven_day": {
            "used_percentage": seven_day_used,
            "remaining_percentage": 100 - seven_day_used,
            "resets_at": current + 400000 if seven_day_resets is None else seven_day_resets,
            "observed_at": current - 5 if seven_day_observed is None else seven_day_observed,
        },
        "updated_at": current - 5,
    }


class QuotaWindowTypingTests(unittest.TestCase):
    def test_window_typing_is_conservative(self):
        self.assertEqual(classify_quota_window(SESSION_LIMIT), "five_hour")
        self.assertEqual(classify_quota_window("5-hour limit reached"), "five_hour")
        self.assertEqual(classify_quota_window(WEEKLY_LIMIT), "seven_day")
        self.assertEqual(classify_quota_window("7-day usage limit reached"), "seven_day")
        # Ambiguous or window-free human text is never guessed at.
        self.assertIsNone(classify_quota_window(AMBIGUOUS_LIMIT))
        self.assertIsNone(classify_quota_window("session limit and weekly limit reached"))
        self.assertIsNone(classify_quota_window(""))
        self.assertIsNone(classify_quota_window(None))

    def test_provenance_records_only_trustworthy_snapshot_resets(self):
        created = 1_000_000.0
        limits = {
            "five_hour": {"resets_at": created + 3240, "observed_at": created - 30},
        }
        record = quota_cooldown_provenance(
            SESSION_LIMIT, limits, created, usage_stale_seconds=STALE
        )
        self.assertEqual(record["kind"], "provider-quota")
        self.assertEqual(record["window"], "five_hour")
        self.assertEqual(record["window_resets_at"], created + 3240)
        self.assertEqual(record["window_observed_at"], created - 30)
        # The human "resets 22:30" text never becomes a bound on its own.
        ambiguous = quota_cooldown_provenance(
            AMBIGUOUS_LIMIT, limits, created, usage_stale_seconds=STALE
        )
        self.assertIsNone(ambiguous["window"])
        self.assertIsNone(ambiguous["window_resets_at"])
        for bad_reset in (
            None,
            "22:30",
            float("nan"),
            float("inf"),
            created - 1,
            created,
            created + 7 * 3600,
            created + 9 * 86400,
            True,
        ):
            with self.subTest(bad_reset=bad_reset):
                record = quota_cooldown_provenance(
                    SESSION_LIMIT,
                    {"five_hour": {"resets_at": bad_reset, "observed_at": created - 30}},
                    created,
                    usage_stale_seconds=STALE,
                )
                self.assertIsNone(record["window_resets_at"])
        # A reset backed by a missing, stale, or future observation is not
        # trustworthy provenance even when the epoch itself looks plausible.
        for bad_observed in (None, True, created - STALE - 1, created + 5):
            with self.subTest(bad_observed=bad_observed):
                record = quota_cooldown_provenance(
                    SESSION_LIMIT,
                    {"five_hour": {"resets_at": created + 3240, "observed_at": bad_observed}},
                    created,
                    usage_stale_seconds=STALE,
                )
                self.assertIsNone(record["window_resets_at"])
                self.assertIsNone(record["window_observed_at"])
        missing = quota_cooldown_provenance(
            SESSION_LIMIT, {}, created, usage_stale_seconds=STALE
        )
        self.assertEqual(missing["window"], "five_hour")
        self.assertIsNone(missing["window_resets_at"])

    def test_bound_uses_snapshot_reset_and_never_extends_the_fallback(self):
        created, fallback = 1_000_000.0, 1_018_000.0
        bounded = bounded_cooldown_until(provenance(created, created + 3240), fallback)
        self.assertEqual(bounded, created + 3240 + RESET_GRACE_SECONDS)
        for untrusted in (
            provenance(created, None),
            provenance(created, float("nan")),
            provenance(created, created - 10),
            provenance(created, created + 9 * 86400),
            {"kind": "other", "created_at": created, "window_resets_at": created + 60},
            None,
        ):
            with self.subTest(untrusted=untrusted):
                self.assertEqual(bounded_cooldown_until(untrusted, fallback), fallback)
        # Trusted evidence only shortens the wait: a five-hour reset past the
        # fallback and a valid multi-day weekly reset both keep the fallback.
        late = provenance(created, created + 19800)
        self.assertEqual(bounded_cooldown_until(late, fallback), fallback)
        weekly = {**provenance(created, created + 4 * 86400), "window": "seven_day"}
        self.assertEqual(bounded_cooldown_until(weekly, fallback), fallback)


class QuotaEvidenceExtractionTests(unittest.TestCase):
    def setUp(self):
        self.current = 2_000_000.0

    def test_global_timestamp_is_never_synthesized_into_windows(self):
        payload = {
            "updated_at": self.current - 5,
            "five_hour": {"used_percentage": 10.0, "resets_at": self.current + 3000},
            "seven_day": {"used_percentage": 25.0, "resets_at": self.current + 400000},
        }
        self.assertEqual(quota_evidence_from_snapshot(payload, self.current), {})
        # The ordinary usage reader deliberately keeps its legacy synthesis.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "claude-usage.json").write_text(json.dumps(payload))
            legacy = load_claude_rate_limits(root, self.current)
            self.assertEqual(legacy["five_hour"]["observed_at"], self.current - 5)

    def test_only_genuine_finite_values_survive(self):
        base = windows(self.current)
        evidence = quota_evidence_from_snapshot(base, self.current)
        self.assertEqual(set(evidence), {"five_hour", "seven_day"})
        self.assertEqual(evidence["five_hour"]["observed_at"], self.current - 5)
        self.assertEqual(evidence["five_hour"]["resets_at"], self.current + 17000)
        for name, payload in {
            "future observation": windows(
                self.current, five_hour_observed=self.current + 30
            ),
            "boolean observation": windows(self.current, five_hour_observed=True),
            "boolean percentage": windows(self.current, five_hour_used=True),
            "string percentage": {
                **windows(self.current),
                "five_hour": {
                    "used_percentage": "10",
                    "resets_at": self.current + 3000,
                    "observed_at": self.current - 5,
                },
            },
            "nan percentage": windows(self.current, five_hour_used=math.nan),
            "boolean reset": windows(self.current, five_hour_resets=True),
            "infinite reset": windows(self.current, five_hour_resets=math.inf),
            "past reset": windows(self.current, five_hour_resets=self.current - 10),
            "implausible five-hour reset": windows(
                self.current, five_hour_resets=self.current + 86400
            ),
        }.items():
            with self.subTest(name=name):
                evidence = quota_evidence_from_snapshot(payload, self.current)
                self.assertNotIn("five_hour", evidence)
                self.assertIn("seven_day", evidence)
        weekly = windows(self.current, seven_day_resets=self.current + 30 * 86400)
        self.assertNotIn(
            "seven_day", quota_evidence_from_snapshot(weekly, self.current)
        )
        # An absent reset is a legitimate idle window, not corruption.
        no_reset = windows(self.current)
        no_reset["five_hour"]["resets_at"] = None
        evidence = quota_evidence_from_snapshot(no_reset, self.current)
        self.assertIsNone(evidence["five_hour"]["resets_at"])


class QuotaRecoveryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.current = time.time()
        self.created = self.current - 3600
        self.recorded_reset = self.current - 300
        self.record = provenance(self.created, self.recorded_reset)

    def recovered(self, record, limits) -> bool:
        return quota_cooldown_recovered(
            record, limits, at=self.current, usage_stale_seconds=STALE
        )

    def test_fresh_post_reset_snapshot_recovers(self):
        self.assertTrue(self.recovered(self.record, windows(self.current)))

    def test_observation_after_recorded_reset_recovers_without_new_cycle(self):
        limits = windows(
            self.current,
            five_hour_resets=self.recorded_reset + 17000,
            five_hour_observed=self.recorded_reset + 30,
        )
        self.assertTrue(self.recovered(self.record, limits))

    def test_rejects_stale_partial_and_unrelated_freshness(self):
        cases = {
            "observed before failure": windows(
                self.current, five_hour_observed=self.created - 100
            ),
            "observed at failure": windows(
                self.current, five_hour_observed=self.created
            ),
            "observed in the future": windows(
                self.current, five_hour_observed=self.current + 30
            ),
            "stale affected window": windows(
                self.current, five_hour_observed=self.current - STALE - 60
            ),
            "stale other window": windows(
                self.current, seven_day_observed=self.current - STALE - 60
            ),
            "weekly-only refresh": windows(
                self.current,
                five_hour_resets=self.current + 17000,
                five_hour_observed=self.created - 100,
            ),
            "missing seven_day": {
                key: value
                for key, value in windows(self.current).items()
                if key != "seven_day"
            },
            "missing five_hour": {
                key: value
                for key, value in windows(self.current).items()
                if key != "five_hour"
            },
            "exhausted five_hour": windows(self.current, five_hour_used=100.0),
            "exhausted seven_day": windows(self.current, seven_day_used=100.0),
            "boolean percentage": windows(self.current, five_hour_used=True),
            "no fresh reset epoch": windows(self.current, five_hour_resets=math.nan),
            "past fresh reset": windows(
                self.current, five_hour_resets=self.current - 10
            ),
            "implausible fresh reset": windows(
                self.current, five_hour_resets=self.current + 7 * 3600
            ),
            "implausible other reset": windows(
                self.current, seven_day_resets=self.current + 30 * 86400
            ),
            "empty snapshot": {},
            "not a snapshot": None,
        }
        cases["no fresh reset epoch"]["five_hour"]["resets_at"] = None
        # An unrelated newer global timestamp must not vouch for the window.
        unrelated = windows(self.current, five_hour_observed=self.created - 100)
        unrelated["five_hour"]["resets_at"] = self.current + 17000
        unrelated["updated_at"] = self.current - 1
        cases["unrelated global timestamp"] = unrelated
        for name, limits in cases.items():
            with self.subTest(name=name):
                self.assertFalse(self.recovered(self.record, limits))

    def test_rejects_records_without_trustworthy_provenance(self):
        good = windows(self.current)
        for name, record in {
            "no provenance": None,
            "wrong kind": {**self.record, "kind": "model-cap"},
            "untyped window": {**self.record, "window": None},
            "unknown window": {**self.record, "window": "monthly"},
            "no recorded reset": {**self.record, "window_resets_at": None},
            "no creation time": {**self.record, "created_at": None},
            "nan creation time": {**self.record, "created_at": float("nan")},
            "future creation time": provenance(
                self.current + 100, self.current + 200
            ),
            "no recorded observation": provenance(
                self.created, self.recorded_reset, None
            ),
            "stale recorded observation": provenance(
                self.created, self.recorded_reset, self.created - STALE - 60
            ),
            "future recorded observation": provenance(
                self.created, self.recorded_reset, self.created + 10
            ),
            "implausible recorded reset": provenance(
                self.created, self.created + 9 * 86400
            ),
        }.items():
            with self.subTest(name=name):
                self.assertFalse(self.recovered(record, good))


class RefreshTransientRecoveryTests(unittest.TestCase):
    def write_usage(self, root: Path, limits: dict) -> None:
        (root / "claude-usage.json").write_text(json.dumps(limits))

    def settings(self) -> dict:
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        return settings

    def assert_persisted_snapshot_recovers(
        self, snapshot: dict, recovers: bool, settings: dict | None = None
    ) -> None:
        current = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_usage(root, snapshot)
            write_settings(root, settings)
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                provider["status"] = "cooldown"
                provider["status_until"] = current + 18000
                provider["cooldown_provenance"] = provenance(current - 3600, current - 300)
                refresh_transients(data, root, settings or self.settings())
                self.assertEqual(provider["status"], "ok" if recovers else "cooldown")

    def test_typed_cooldown_recovers_and_preserves_last_error(self):
        current = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_usage(root, windows(current))
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                provider["status"] = "cooldown"
                provider["status_until"] = current + 18000
                provider["last_error"] = "session limit reached"
                provider["cooldown_provenance"] = provenance(current - 3600, current - 300)
                refresh_transients(data, root, self.settings())
                self.assertEqual(provider["status"], "ok")
                self.assertIsNone(provider["status_until"])
                self.assertIsNone(provider["cooldown_provenance"])
                self.assertEqual(provider["last_error"], "session limit reached")

    def test_cooldown_without_provenance_stays_conservative(self):
        current = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_usage(root, windows(current))
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                provider["status"] = "cooldown"
                provider["status_until"] = current + 18000
                refresh_transients(data, root, self.settings())
                self.assertEqual(provider["status"], "cooldown")
                self.assertEqual(provider["status_until"], current + 18000)

    def test_global_only_snapshot_does_not_recover(self):
        # The reproduced bug: a snapshot carrying only the global updated_at
        # and no per-window observed_at must not vouch for any window.
        current = time.time()
        self.assert_persisted_snapshot_recovers(
            {
                "updated_at": current - 5,
                "five_hour": {"used_percentage": 0.0, "resets_at": current + 17000},
                "seven_day": {"used_percentage": 25.0, "resets_at": current + 400000},
            },
            recovers=False,
        )

    def test_future_window_observation_does_not_recover(self):
        # The second reproduced bug: an observed_at from the future is not
        # evidence of anything.
        current = time.time()
        self.assert_persisted_snapshot_recovers(
            windows(current, five_hour_observed=current + 300), recovers=False
        )

    def test_rejects_corrupt_or_partial_persisted_evidence(self):
        current = time.time()
        created = current - 3600
        missing_other_timestamp = windows(current)
        del missing_other_timestamp["seven_day"]["observed_at"]
        cases = {
            "missing other-window timestamp": missing_other_timestamp,
            "stale other window": windows(
                current, seven_day_observed=current - STALE - 120
            ),
            "observed before failure": windows(
                current, five_hour_observed=created - 100
            ),
            "observed at failure": windows(current, five_hour_observed=created),
            "boolean percentage": windows(current, five_hour_used=True),
            "nan observation": windows(current, five_hour_observed=math.nan),
            "overflowing integer observation": windows(
                current, five_hour_observed=10**400
            ),
            "infinite reset": windows(current, five_hour_resets=math.inf),
            "past reset": windows(current, five_hour_resets=current - 10),
            "implausible five-hour reset": windows(
                current, five_hour_resets=current + 86400
            ),
            "implausible seven-day reset": windows(
                current, seven_day_resets=current + 30 * 86400
            ),
        }
        for name, snapshot in cases.items():
            with self.subTest(name=name):
                self.assert_persisted_snapshot_recovers(snapshot, recovers=False)
        self.assert_persisted_snapshot_recovers(windows(current), recovers=True)

    def test_recovery_respects_configured_freshness_policy(self):
        current = time.time()
        strict = self.settings()
        strict["providers"]["claude"]["usage_stale_seconds"] = 60
        snapshot = windows(
            current, five_hour_observed=current - 120, seven_day_observed=current - 120
        )
        self.assert_persisted_snapshot_recovers(
            snapshot, recovers=False, settings=strict
        )
        self.assert_persisted_snapshot_recovers(snapshot, recovers=True)

    def test_untrustworthy_recorded_provenance_never_recovers(self):
        current = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_usage(root, windows(current))
            for name, record in {
                "legacy record without observation": provenance(
                    current - 3600, current - 300, None
                ),
                "record created from stale evidence": provenance(
                    current - 3600, current - 300, current - 3600 - STALE - 60
                ),
            }.items():
                with self.subTest(name=name), Ledger(root) as data:
                    provider = data["providers"]["claude"]
                    provider["status"] = "cooldown"
                    provider["status_until"] = current + 18000
                    provider["cooldown_provenance"] = record
                    refresh_transients(data, root, self.settings())
                    self.assertEqual(provider["status"], "cooldown")

    def test_stale_snapshot_does_not_recover_but_expiry_still_does(self):
        current = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_usage(
                root, windows(current, five_hour_observed=current - 7200)
            )
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                provider["status"] = "cooldown"
                provider["status_until"] = current + 18000
                provider["cooldown_provenance"] = provenance(current - 3600, current - 300)
                refresh_transients(data, root, self.settings())
                self.assertEqual(provider["status"], "cooldown")
                provider["status_until"] = current - 1
                refresh_transients(data, root, self.settings())
                self.assertEqual(provider["status"], "ok")
                self.assertIsNone(provider["cooldown_provenance"])

    def test_never_clears_nonquota_states_or_model_caps(self):
        current = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_usage(root, windows(current))
            record = provenance(current - 3600, current - 300)
            for status in ("needs-auth", "missing"):
                with self.subTest(status=status), Ledger(root) as data:
                    provider = data["providers"]["claude"]
                    provider["status"] = status
                    provider["status_until"] = None
                    provider["cooldown_provenance"] = dict(record)
                    refresh_transients(data, root, self.settings())
                    self.assertEqual(provider["status"], status)
            off = registry_settings()
            write_settings(root, off)
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                provider["status"] = "cooldown"
                provider["status_until"] = current + 18000
                provider["cooldown_provenance"] = dict(record)
                refresh_transients(data, root, off)
                self.assertEqual(provider["status"], "off")
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                provider["status"] = "cooldown"
                provider["status_until"] = current + 18000
                provider["cooldown_provenance"] = dict(record)
                provider["models"]["fable"] = {
                    "status": "capped",
                    "status_until": current + 9000,
                    "last_error": "reached your Fable limit",
                }
                refresh_transients(data, root, self.settings())
                self.assertEqual(provider["status"], "ok")
                self.assertEqual(provider["models"]["fable"]["status"], "capped")


class ProviderCooldownCreationTests(unittest.TestCase):
    """Reproduce the 22:30-reset-versus-02:36-fallback session failure."""

    def run_failing(self, root: Path, error: str, execute=None):
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "manual_only"
        repo = root / "repo"
        repo.mkdir(exist_ok=True)

        def limited(command, output, *_args, **_kwargs):
            payload = {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "result": error,
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(payload))
            return "failed", json.dumps(payload), 1, 1

        request = {
            "prompt": "review",
            "kind": "review",
            "size": "small",
            "_capacity_checked": True,
            "cd": str(repo),
        }
        with (
            mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/claude"),
            mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
            mock.patch("codex_seats.workers._execute", side_effect=execute or limited),
        ):
            return run_claude(root, settings, request)

    def test_session_cooldown_is_bounded_by_snapshot_reset(self):
        current = time.time()
        # Mirrors the observed failure: 21:36 failure, provider reset at 22:30,
        # legacy fallback would have waited until 02:36 (now + 18000).
        reset = current + 3240
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "claude-usage.json").write_text(
                json.dumps(
                    {
                        "updated_at": current - 60,
                        "five_hour": {
                            "used_percentage": 100.0,
                            "resets_at": reset,
                            "observed_at": current - 60,
                        },
                        "seven_day": {
                            "used_percentage": 25.0,
                            "resets_at": current + 400000,
                            "observed_at": current - 60,
                        },
                    }
                )
            )
            result = self.run_failing(root, SESSION_LIMIT)
            self.assertEqual(result.status, "capacity")
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                self.assertEqual(provider["status"], "cooldown")
                self.assertEqual(provider["status_until"], reset + RESET_GRACE_SECONDS)
                self.assertLess(provider["status_until"], current + 18000 - 3600)
                record = provider["cooldown_provenance"]
                self.assertEqual(record["kind"], "provider-quota")
                self.assertEqual(record["window"], "five_hour")
                self.assertEqual(record["window_resets_at"], reset)
                self.assertEqual(record["window_observed_at"], current - 60)
                self.assertIn("session limit", provider["last_error"])

    def test_weekly_reset_never_extends_beyond_the_fallback(self):
        current = time.time()
        reset = current + 400000
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "claude-usage.json").write_text(
                json.dumps(
                    windows(
                        current,
                        seven_day_used=100.0,
                        seven_day_resets=reset,
                        five_hour_observed=current - 60,
                        seven_day_observed=current - 60,
                    )
                )
            )
            before = time.time()
            result = self.run_failing(root, WEEKLY_LIMIT)
            after = time.time()
            self.assertEqual(result.status, "capacity")
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                self.assertEqual(provider["status"], "cooldown")
                # Trusted weekly evidence is recorded but must not stretch the
                # local wait into a multi-day cooldown.
                self.assertGreaterEqual(provider["status_until"], before + 18000)
                self.assertLessEqual(provider["status_until"], after + 18000)
                record = provider["cooldown_provenance"]
                self.assertEqual(record["window"], "seven_day")
                self.assertEqual(record["window_resets_at"], reset)

    def test_untrustworthy_observations_keep_fallback_at_creation(self):
        current = time.time()
        for name, snapshot in {
            "global-only timestamps": {
                "updated_at": current - 60,
                "five_hour": {"used_percentage": 100.0, "resets_at": current + 3240},
                "seven_day": {"used_percentage": 25.0, "resets_at": current + 400000},
            },
            "stale affected observation": windows(
                current,
                five_hour_used=100.0,
                five_hour_resets=current + 3240,
                five_hour_observed=current - STALE - 3600,
            ),
        }.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "claude-usage.json").write_text(json.dumps(snapshot))
                before = time.time()
                result = self.run_failing(root, SESSION_LIMIT)
                after = time.time()
                self.assertEqual(result.status, "capacity")
                with Ledger(root) as data:
                    provider = data["providers"]["claude"]
                    self.assertEqual(provider["status"], "cooldown")
                    self.assertGreaterEqual(provider["status_until"], before + 18000)
                    self.assertLessEqual(provider["status_until"], after + 18000)
                    record = provider["cooldown_provenance"]
                    self.assertIsNone(record["window_resets_at"])
                    self.assertIsNone(record["window_observed_at"])

    def test_unknown_windows_keep_the_finite_fallback(self):
        for name, error, snapshot in (
            ("no snapshot", SESSION_LIMIT, None),
            ("ambiguous human reset text", AMBIGUOUS_LIMIT, {
                "updated_at": time.time() - 60,
                "five_hour": {
                    "used_percentage": 100.0,
                    "resets_at": time.time() + 3240,
                    "observed_at": time.time() - 60,
                },
            }),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if snapshot is not None:
                    (root / "claude-usage.json").write_text(json.dumps(snapshot))
                before = time.time()
                result = self.run_failing(root, error)
                after = time.time()
                self.assertEqual(result.status, "capacity")
                with Ledger(root) as data:
                    provider = data["providers"]["claude"]
                    self.assertEqual(provider["status"], "cooldown")
                    self.assertGreaterEqual(provider["status_until"], before + 18000)
                    self.assertLessEqual(provider["status_until"], after + 18000)
                    record = provider["cooldown_provenance"]
                    self.assertEqual(record["kind"], "provider-quota")
                    self.assertIsNone(record["window_resets_at"])

    def test_model_cap_does_not_create_provider_quota_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_failing(root, "You've reached your Sonnet limit")
            self.assertEqual(result.status, "capacity")
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                self.assertNotEqual(provider["status"], "cooldown")
                self.assertIsNone(provider["cooldown_provenance"])
                self.assertEqual(
                    provider["models"]["claude-sonnet-5"]["status"], "capped"
                )

    def test_needs_auth_and_missing_clear_stale_provenance(self):
        current = time.time()

        def missing_executable(command, output, *_args, **_kwargs):
            raise FileNotFoundError("claude executable not found")

        for name, error, execute, status in (
            ("needs-auth", "not logged in", None, "needs-auth"),
            ("missing", "unused", missing_executable, "missing"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with Ledger(root) as data:
                    data["providers"]["claude"]["cooldown_provenance"] = provenance(
                        current - 3600, current - 300
                    )
                self.run_failing(root, error, execute=execute)
                with Ledger(root) as data:
                    provider = data["providers"]["claude"]
                    self.assertEqual(provider["status"], status)
                    self.assertIsNone(provider["cooldown_provenance"])


class OperatorResetStateTests(unittest.TestCase):
    def test_reset_state_clears_cooldown_provenance(self):
        current = time.time()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_settings(root, registry_settings())
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                provider["status"] = "cooldown"
                provider["status_until"] = current + 18000
                provider["last_error"] = "session limit reached"
                provider["cooldown_provenance"] = provenance(current - 3600, current - 300)
            args = SimpleNamespace(
                provider_name="claude",
                mode="manual_only",
                from_current_user_request=True,
                expires_at=None,
                reset_state=True,
                cancel_active=False,
            )
            with (
                mock.patch("codex_seats.cli.ROOT", root),
                mock.patch.dict(
                    os.environ, {"CODEX_SEATS_MANAGED_WORKER": "0"}, clear=True
                ),
                mock.patch("sys.stdin.isatty", return_value=True),
            ):
                self.assertEqual(cmd_provider_set(args, registry_settings()), 0)
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                self.assertEqual(provider["status"], "ok")
                self.assertIsNone(provider["status_until"])
                self.assertIsNone(provider["cooldown_provenance"])
                self.assertIsNone(provider["last_error"])


class AdmissionRecoveryTests(unittest.TestCase):
    def test_admission_recovers_only_after_genuine_reset_evidence(self):
        current = time.time()
        settings = registry_settings()
        settings["providers"]["claude"]["mode"] = "manual_only"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            write_settings(root, settings)
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                provider["status"] = "cooldown"
                provider["status_until"] = current + 18000
                provider["last_error"] = "session limit reached"
                provider["cooldown_provenance"] = provenance(current - 3600, current - 300)

            def successful(command, output, *_args, **_kwargs):
                payload = {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
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

            request = {
                "prompt": "review",
                "kind": "review",
                "size": "small",
                "_capacity_checked": True,
                "cd": str(repo),
            }
            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/claude"),
                mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                mock.patch("codex_seats.workers._execute", side_effect=successful),
            ):
                # Without fresh evidence the stale cooldown still rejects work.
                self.assertEqual(run_claude(root, settings, request).status, "unavailable")
                (root / "claude-usage.json").write_text(json.dumps(windows(current)))
                recovered = run_claude(root, settings, request)
            self.assertEqual(recovered.status, "completed")
            with Ledger(root) as data:
                provider = data["providers"]["claude"]
                self.assertEqual(provider["status"], "ok")
                self.assertIsNone(provider["status_until"])
                self.assertIsNone(provider["cooldown_provenance"])


if __name__ == "__main__":
    unittest.main()
