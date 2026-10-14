from __future__ import annotations

import argparse
import time
import unittest
from datetime import datetime, timezone

from codex_seats import dispatch_cli
from codex_seats import dispatch_protocol
from codex_seats.capacity import (
    add_capacity_sample,
    capacity_task_fits,
    capacity_window_signature,
    learning_key,
    rolling_task_fits,
    seat_capacity_report,
)
from codex_seats.provider_claude import claude_model_candidates
from codex_seats.router import choose_provider
from codex_seats.state import default_settings, new_seat
from codex_seats.task_policy import (
    MAX_RUN_TIMEOUT_SECONDS,
    claude_provider_eligible,
    resolve_task_policy,
    timeout_seconds,
)
from codex_seats.telemetry import apply_usage_limits


def _rolling_seat(*, weekly: float, five_hour: float | None = None) -> dict:
    limits = {"weekly": {"pct": weekly}}
    if five_hour is not None:
        limits["5h"] = {"pct": five_hour}
    return {
        "usage_updated_at": datetime.now(timezone.utc).isoformat(),
        "last_probe_limits": limits,
    }


def _fits(seat: dict, size: str, *, learning: dict | None = None) -> bool:
    settings = default_settings()
    return rolling_task_fits(
        seat,
        size,
        current=time.time(),
        stale_seconds=settings["pool"]["probe_stale_seconds"],
        settings=settings,
        learning=learning,
        model="gpt-5.6-sol",
        effort="high",
    )[0]


class TaskPolicyTests(unittest.TestCase):
    def test_missing_validation_is_unspecified_and_invalid_facts_are_rejected(self):
        policy = resolve_task_policy(default_settings(), "implementation")
        self.assertEqual(policy.validation, "unspecified")
        with self.assertRaisesRegex(ValueError, "task_shape"):
            resolve_task_policy(
                default_settings(), "implementation", task_shape="caller-choice"
            )
        with self.assertRaisesRegex(ValueError, "validation"):
            resolve_task_policy(
                default_settings(), "implementation", validation="caller-choice"
            )

    def test_bounded_research_is_small_complex_medium_and_claude_eligible(self):
        policy = resolve_task_policy(default_settings(), "research")
        self.assertEqual(policy.size, "small")
        self.assertEqual(policy.capability, "complex")
        self.assertEqual(policy.effort, "medium")
        self.assertEqual(policy.preferred_provider, "claude")
        self.assertEqual(policy.codex_model, "gpt-6-sol")
        self.assertTrue(policy.claude_eligible)
        self.assertEqual(
            claude_model_candidates(default_settings(), "research", "small"),
            ["claude-opus-5-5", "claude-sonnet-5"],
        )
        self.assertFalse(
            claude_provider_eligible(
                policy, "opportunistic", capacity_available=False
            )
        )
        self.assertTrue(
            claude_provider_eligible(
                policy, "opportunistic", capacity_available=True
            )
        )

    def test_deep_research_is_big_frontier_high_and_claude_eligible(self):
        policy = resolve_task_policy(default_settings(), "deep_research")
        self.assertEqual(
            (policy.size, policy.capability, policy.effort, policy.codex_model),
            ("big", "frontier", "high", "gpt-6-astra"),
        )
        self.assertEqual(policy.preferred_provider, "claude")
        self.assertTrue(policy.claude_eligible)
        self.assertEqual(
            claude_model_candidates(default_settings(), "deep_research", "big"),
            ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5"],
        )
        self.assertFalse(
            claude_provider_eligible(
                policy, "opportunistic", capacity_available=False
            )
        )
        self.assertTrue(
            claude_provider_eligible(
                policy, "opportunistic", capacity_available=True
            )
        )

    def test_retuned_authoring_efforts_preserve_high_risk_tiers(self):
        settings = default_settings()
        expected = {
            "mechanical": "low",
            "docs": "low",
            "test": "medium",
            "implementation": "high",
            "debug": "high",
            "ui_ux": "high",
            "deep_research": "high",
            "review": "medium",
            "research": "medium",
            "planning": "high",
            "audit": "high",
            "security": "max",
        }
        self.assertEqual(settings["execution"]["effort_by_kind"], expected)
        for kind, effort in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(resolve_task_policy(settings, kind).effort, effort)

    def test_dispatch_protocol_accepts_research_but_stays_strict(self):
        settings = {"projects": {"repo": {"path": "/registered/repo"}}}
        request = {
            "schema": dispatch_protocol.SCHEMA_VERSION,
            "release_id": "a" * 64,
            "protocol_version": dispatch_protocol.PROTOCOL_VERSION,
            "protocol_min": dispatch_protocol.PROTOCOL_VERSION,
            "protocol_max": dispatch_protocol.PROTOCOL_VERSION,
            "request_id": dispatch_protocol.new_request_id(),
            "operation": "task",
            "repo_id": "repo",
            "prompt": "study the implementation",
            "provider": "auto",
            "kind": "research",
            "size": "auto",
            "origin": {"head": "codex"},
            "created_at": 100.0,
            "expires_at": 200.0,
        }
        dispatch_protocol.validate_request(request, settings, now=150.0)
        with self.assertRaises(dispatch_protocol.DispatchError):
            dispatch_protocol.validate_request(
                {**request, "kind": "invented"}, settings, now=150.0
            )
        with self.assertRaises(dispatch_protocol.DispatchError):
            dispatch_protocol.validate_request(
                {**request, "capability_tier": "routine"},
                settings,
                now=150.0,
            )

    def test_codex_model_matrix_is_work_shaped_and_risk_promotes(self):
        settings = default_settings()
        expected = {
            "mechanical": "gpt-5.6-luna",
            "docs": "gpt-5.6-terra",
            "test": "gpt-5.6-terra",
            "implementation": "gpt-6-sol",
            "ui_ux": "gpt-6-astra",
            "planning": "gpt-6-astra",
        }
        for kind, model in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(resolve_task_policy(settings, kind).codex_model, model)
        self.assertEqual(
            resolve_task_policy(settings, "docs", risk_flags=["security"]).codex_model,
            "gpt-6-astra",
        )

    def test_ui_ux_is_a_first_class_big_frontier_authoring_kind(self):
        policy = resolve_task_policy(default_settings(), "ui_ux")
        self.assertEqual(
            (policy.size, policy.capability, policy.effort, policy.codex_model),
            ("big", "frontier", "high", "gpt-6-astra"),
        )
        self.assertTrue(policy.claude_eligible)

    def test_size_override_does_not_change_capability_tier(self):
        settings = default_settings()
        self.assertEqual(
            claude_model_candidates(settings, "implementation", "small"),
            ["claude-opus-5-5", "claude-sonnet-5"],
        )
        self.assertEqual(
            claude_model_candidates(settings, "docs", "big"), ["claude-sonnet-5"]
        )

    def test_legacy_claude_role_defaults_upgrade_to_current_ids(self):
        settings = default_settings()
        settings["providers"]["claude"]["model_roles"] = {
            "frontier": "claude-fable-5",
            "complex": "opus",
            "routine": "sonnet",
        }
        self.assertEqual(
            claude_model_candidates(settings, "security", "big"),
            ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5"],
        )
        self.assertEqual(
            claude_model_candidates(
                settings,
                "security",
                "big",
                explicit_model="claude-fable-5",
            ),
            ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5"],
        )

    def test_explicit_override_requires_a_reason_at_the_runner_boundary(self):
        with self.assertRaisesRegex(ValueError, "override_reason"):
            resolve_task_policy(
                default_settings(),
                "docs",
                size="big",
                require_override_reason=True,
            )
        profile = resolve_task_policy(
            default_settings(),
            "docs",
            size="big",
            override_reason="multi-package docs migration",
            require_override_reason=True,
        )
        self.assertEqual(profile.size, "big")
        self.assertEqual(profile.override_reason, "multi-package docs migration")

        frontier_research = resolve_task_policy(
            default_settings(),
            "research",
            size="big",
            capability="frontier",
            override_reason="bounded investigation exposed architectural uncertainty",
            require_override_reason=True,
        )
        self.assertEqual(
            (
                frontier_research.size,
                frontier_research.capability,
                frontier_research.codex_model,
            ),
            ("big", "frontier", "gpt-6-astra"),
        )

        with self.assertRaisesRegex(ValueError, "effort override requires"):
            resolve_task_policy(
                default_settings(),
                "ui_ux",
                effort="max",
                require_override_reason=True,
            )
        max_ui = resolve_task_policy(
            default_settings(),
            "ui_ux",
            effort="max",
            override_reason="high-stakes interaction",
            require_override_reason=True,
        )
        self.assertEqual(max_ui.effort, "max")

    def test_risk_promotes_automatic_size_and_capability(self):
        profile = resolve_task_policy(
            default_settings(),
            "docs",
            size="auto",
            risk_flags=["permissions"],
        )
        self.assertEqual(profile.size, "big")
        self.assertEqual(profile.capability, "frontier")
        self.assertEqual(profile.effort, "low")

    def test_explicit_small_permission_task_preserves_scope_and_risk(self):
        for minutes in (7, 20):
            with self.subTest(minutes=minutes):
                profile = resolve_task_policy(
                    default_settings(), "implementation", size="small",
                    estimated_minutes=minutes, risk_flags=["permissions"],
                    override_reason="bounded policy-core slice",
                    require_override_reason=True,
                )
                self.assertEqual(profile.size, "small")
                self.assertEqual(profile.capability, "frontier")
                self.assertEqual(profile.risk_flags, ("permissions",))
                self.assertEqual(profile.effort, "high")

    def test_small_risk_override_requires_reason_even_for_small_default(self):
        with self.assertRaisesRegex(ValueError, "override_reason"):
            resolve_task_policy(
                default_settings(), "docs", size="small",
                risk_flags=["permissions"], require_override_reason=True,
            )

    def test_timeout_uses_estimate_not_size_as_a_model_proxy(self):
        short = resolve_task_policy(
            default_settings(), "implementation", estimated_minutes=3
        )
        long = resolve_task_policy(
            default_settings(), "implementation", estimated_minutes=30
        )
        self.assertEqual(timeout_seconds(short), 600)
        self.assertEqual(timeout_seconds(long), 3600)

    def test_long_estimate_has_finite_policy_timeout_without_old_30m_ceiling(self):
        long = resolve_task_policy(
            default_settings(), "implementation", estimated_minutes=480
        )
        self.assertEqual(timeout_seconds(long), 57_600)
        self.assertLessEqual(timeout_seconds(long), MAX_RUN_TIMEOUT_SECONDS)

        beyond_envelope = resolve_task_policy(
            default_settings(), "implementation", estimated_minutes=10_000
        )
        self.assertEqual(
            timeout_seconds(beyond_envelope), MAX_RUN_TIMEOUT_SECONDS
        )

    def test_delegate_parser_accepts_bounded_and_deep_research(self):
        parser = argparse.ArgumentParser()
        dispatch_cli.register(
            parser.add_subparsers(dest="command", required=True)
        )
        self.assertEqual(
            parser.parse_args(
                ["delegate", "study", "--kind", "research"]
            ).kind,
            "research",
        )
        self.assertEqual(
            parser.parse_args(
                ["delegate", "investigate", "--kind", "deep_research"]
            ).kind,
            "deep_research",
        )

    def test_research_provider_eligibility_tracks_live_capacity(self):
        settings = default_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        data = {
            "providers": {
                "claude": {
                    "status": "ok",
                    "in_flight": None,
                    "models": {},
                }
            }
        }

        def limits(remaining: float) -> dict:
            return {
                "updated_at": time.time(),
                "five_hour": {"remaining_percentage": remaining},
                "seven_day": {"remaining_percentage": remaining},
            }

        self.assertEqual(
            choose_provider(
                "auto",
                "research",
                settings,
                data,
                claude_limits=limits(80),
            )[0],
            "claude",
        )
        self.assertEqual(
            choose_provider(
                "auto",
                "research",
                settings,
                data,
                claude_limits=limits(9),
            )[0],
            "codex",
        )

    def test_weekly_13_without_five_hour_fits_small_but_not_big(self):
        seat = _rolling_seat(weekly=13)
        self.assertTrue(_fits(seat, "small"))
        self.assertFalse(_fits(seat, "big"))

    def test_complete_fresh_weekly_only_observation_is_authoritative(self):
        observed = time.time()
        seat = _rolling_seat(weekly=13)
        seat["active_window_observation"] = {
            "source": "account",
            "complete": True,
            "observed_at": observed,
            "observed_windows": ["weekly"],
            "active_windows": ["weekly"],
        }
        details: dict = {}
        fits, reason = capacity_task_fits(
            seat,
            "small",
            current=observed,
            stale_seconds=default_settings()["pool"]["probe_stale_seconds"],
            settings=default_settings(),
            details=details,
        )
        self.assertTrue(fits)
        self.assertEqual(details["tier"], "fresh")
        self.assertEqual(details["binding_window"], "weekly")
        self.assertNotIn("rolling", reason.lower())
        self.assertNotIn("W ", reason)

    def test_returned_active_five_hour_constrains_again(self):
        self.assertTrue(_fits(_rolling_seat(weekly=13), "small"))
        self.assertFalse(
            _fits(_rolling_seat(weekly=13, five_hour=9), "small")
        )

    def test_legacy_rolling_floors_do_not_override_learned_fit(self):
        settings = default_settings()
        settings["pool"]["rolling_small_task_min_headroom_pct"] = 99
        seat = _rolling_seat(weekly=13)
        self.assertTrue(
            rolling_task_fits(
                seat,
                "small",
                current=time.time(),
                stale_seconds=settings["pool"]["probe_stale_seconds"],
                settings=settings,
                model="gpt-5.6-sol",
                effort="high",
            )[0]
        )

    def test_margin_tapers_near_known_reset_but_burn_does_not(self):
        settings = default_settings()
        current = time.time()
        seat = {
            "usage_updated_at": datetime.now(timezone.utc).isoformat(),
            "last_probe_limits": {
                "weekly": {
                    "pct": 21,
                    "resets_at": current + 60,
                    "window_minutes": 10080,
                }
            },
        }
        fits, reason = rolling_task_fits(
            seat,
            "big",
            current=current,
            stale_seconds=settings["pool"]["probe_stale_seconds"],
            settings=settings,
            model="gpt-5.6-sol",
            effort="high",
        )
        self.assertTrue(fits)
        self.assertIn("margin", reason)

    def test_learned_p90_replaces_fallback_only_after_eight_samples(self):
        seat = _rolling_seat(weekly=12)
        signature = capacity_window_signature(seat, current=time.time())
        self.assertIsNotNone(signature)
        key = learning_key(
            "codex", signature, "gpt-5.6-sol", "small", "high"
        )
        seven: dict = {}
        for burn in [1, 2, 3, 4, 5, 6, 8]:
            add_capacity_sample(
                seven,
                provider="codex",
                cadence=signature,
                model="gpt-5.6-sol",
                size="small",
                effort="high",
                burn_pct=burn,
            )
        eight = {key: {"samples": list(seven[key]["samples"])}}
        add_capacity_sample(
            eight,
            provider="codex",
            cadence=signature,
            model="gpt-5.6-sol",
            size="small",
            effort="high",
            burn_pct=8,
        )
        self.assertEqual(eight[key]["predicted_burn_pct"], 8)
        self.assertTrue(_fits(seat, "small", learning=seven))
        self.assertFalse(_fits(seat, "small", learning=eight))

    def test_legacy_cadence_learning_is_inert(self):
        seat = _rolling_seat(weekly=12)
        legacy = {
            learning_key("codex", "rolling", "gpt-5.6-sol", "small", "high"): {
                "samples": [99] * 8,
                "predicted_burn_pct": 99,
            },
            learning_key("codex", "monthly", "gpt-5.6-sol", "small", "high"): {
                "samples": [99] * 8,
                "predicted_burn_pct": 99,
            },
        }
        self.assertTrue(_fits(seat, "small", learning=legacy))

    def test_learning_is_separated_by_window_signature_and_duration(self):
        current = time.time()
        weekly = _rolling_seat(weekly=12)
        weekly["last_probe_limits"]["weekly"]["window_minutes"] = 10080
        shorter = _rolling_seat(weekly=12)
        shorter["last_probe_limits"]["weekly"]["window_minutes"] = 4320
        signature = capacity_window_signature(weekly, current=current)
        other_signature = capacity_window_signature(shorter, current=current)
        self.assertNotEqual(signature, other_signature)
        learning = {
            learning_key("codex", signature, "gpt-5.6-sol", "small", "high"): {
                "samples": [8] * 8
            }
        }
        self.assertFalse(_fits(weekly, "small", learning=learning))
        self.assertTrue(_fits(shorter, "small", learning=learning))

    def test_degraded_small_has_extra_margin_big_fails_and_grace_expires(self):
        settings = default_settings()
        current = time.time()
        observed = current - 60
        seat = {
            "usage_updated_at": datetime.fromtimestamp(observed, timezone.utc).isoformat(),
            "last_probe_limits": {"monthly": {"pct": 30}},
            "active_window_observation": {
                "source": "account",
                "complete": True,
                "observed_at": observed,
            },
            "account_window_observation": {
                "status": "failed",
                "last_attempt_observed_at": current,
            },
        }
        fits, reason = capacity_task_fits(
            seat, "small", current=current, stale_seconds=3600, settings=settings
        )
        self.assertTrue(fits)
        self.assertIn("tier degraded", reason)
        seat["last_probe_limits"]["monthly"]["pct"] = 19
        self.assertFalse(
            capacity_task_fits(
                seat, "small", current=current, stale_seconds=3600, settings=settings
            )[0]
        )  # 5 burn + 5 normal + 10 degraded margin
        self.assertFalse(
            capacity_task_fits(
                seat, "big", current=current, stale_seconds=3600, settings=settings
            )[0]
        )
        self.assertIn(
            "tier unknown",
            capacity_task_fits(
                seat,
                "small",
                current=(
                    current
                    + 3600
                    + settings["execution"]["capacity"]["degraded_grace_seconds"]
                    + 1
                ),
                stale_seconds=3600,
                settings=settings,
            )[1],
        )

    def test_partial_update_does_not_refresh_an_older_retained_window(self):
        settings = default_settings()
        settings["execution"]["capacity"]["degraded_grace_seconds"] = 600
        current = 10_000.0
        seat = new_seat()
        apply_usage_limits(
            seat,
            {
                "5h": {"pct": 80, "resets_at": 20_000, "window_minutes": 300},
                "weekly": {
                    "pct": 80,
                    "resets_at": 30_000,
                    "window_minutes": 10_080,
                },
            },
            {},
            1_000,
            source="account",
            observation_complete=True,
        )
        apply_usage_limits(
            seat,
            {
                "weekly": {
                    "pct": 79,
                    "resets_at": 30_000,
                    "window_minutes": 10_080,
                }
            },
            {},
            current - 60,
            source="passive",
        )

        fits, reason = capacity_task_fits(
            seat,
            "small",
            current=current,
            stale_seconds=3_600,
            settings=settings,
        )

        self.assertFalse(fits)
        self.assertIn("tier unknown", reason)
        self.assertIn("too old", reason)

    def test_unknown_observation_is_rejected(self):
        fits, reason = capacity_task_fits(
            {"last_probe_limits": {"monthly": {"pct": 100}}},
            "small",
            current=time.time(),
            stale_seconds=3600,
            settings=default_settings(),
        )
        self.assertFalse(fits)
        self.assertIn("tier unknown", reason)

    def test_capacity_report_includes_health_degraded_and_per_seat_floor(self):
        settings = default_settings()
        current = time.time()
        seat = new_seat()
        apply_usage_limits(
            seat,
            {"weekly": {"pct": 40, "window_minutes": 10080}},
            {},
            current,
            source="account",
            observation_complete=True,
        )
        seat["status"] = "ok"
        report = seat_capacity_report("seat3", seat, settings, current=current)
        self.assertTrue(report["fits_small"])
        self.assertTrue(report["fits_big"])
        self.assertEqual(report["tier"], "fresh")

        settings["pool"]["task_min_headroom_pct_by_seat"] = {
            "seat3": {"small": 45, "big": 45}
        }
        floored = seat_capacity_report("seat3", seat, settings, current=current)
        self.assertFalse(floored["fits_small"])
        self.assertFalse(floored["fits_big"])
        self.assertIn("small tasks require 45%", floored["small"]["reason"])

        settings["pool"]["task_min_headroom_pct_by_seat"] = {}
        seat["account_window_observation"].update(
            {"status": "failed", "last_attempt_observed_at": current}
        )
        degraded = seat_capacity_report("seat3", seat, settings, current=current)
        self.assertTrue(degraded["fits_small"])
        self.assertFalse(degraded["fits_big"])
        self.assertEqual(degraded["tier"], "degraded")

        seat["auth_failure"] = "redacted upstream detail"
        unhealthy = seat_capacity_report("seat3", seat, settings, current=current)
        self.assertFalse(unhealthy["auth_ok"])
        self.assertFalse(unhealthy["fits_small"])
        self.assertNotIn("redacted upstream detail", unhealthy["reason"])


if __name__ == "__main__":
    unittest.main()
