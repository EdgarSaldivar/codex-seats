from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_seats.cli import build_parser
from codex_seats.state import Ledger, default_settings, iso
from codex_seats.stats import (
    collect,
    render,
)


class StatsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.current = 2_000_000_000.0

        jobs = [
            {
                "id": "new-codex",
                "ts": iso(self.current - 60),
                "provider": "codex",
                "route_provider": "codex",
                "goal_id": "g1",
                "kind": "implementation",
                "model": "gpt-test",
                "effort": "high",
                "tokens": 100,
                "cost_usd": 0.25,
                "status": "completed",
                "fallback_count": 1,
                "attempted_models": ["gpt-old", "gpt-test"],
                "refused_models": ["gpt-old", "gpt-other"],
                "effort_downgraded_from": "ultra",
            },
            {
                "id": "old-claude",
                "ts": iso(self.current - 120),
                "provider": "claude",
                "kind": "implementation",
                "model": "sonnet",
                "tokens": 300,
                "status": "failed",
            },
        ]
        with Ledger(self.root) as data:
            data["jobs"] = jobs

        archive = self.root / "archive"
        archive.mkdir()
        archived_job = {
            "id": "archived",
            "ts": iso(self.current - 180),
            "provider": "codex",
            "kind": "review",
            "model": "gpt-review",
            "effort": "medium",
            "tokens": 50,
            "status": "completed",
        }
        stale_duplicate = {
            "id": "new-codex",
            "ts": iso(self.current - 240),
            "provider": "claude",
            "kind": "implementation",
            "tokens": 999,
            "status": "failed",
        }
        (archive / "jobs-2033-05.jsonl").write_text(
            json.dumps(stale_duplicate) + "\n" + json.dumps(archived_job) + "\nnot json\n"
        )

        self.settings = default_settings()
        self.settings["projects"] = {"temporary": {"path": str(self.project)}}

    def tearDown(self):
        self._tmp.cleanup()

    def test_collects_routing_and_fallback_metrics(self):
        ledger_before = (self.root / "seats.json").read_bytes()
        report = collect(self.root, self.settings, current=self.current)
        self.assertEqual((self.root / "seats.json").read_bytes(), ledger_before)

        providers = report["routing"]["by_provider"]
        self.assertEqual(providers["codex"]["jobs"], 2)
        self.assertEqual(providers["codex"]["tokens"], 150)
        self.assertEqual(providers["codex"]["cost_usd"], 0.25)
        self.assertEqual(providers["claude"]["jobs"], 1)
        self.assertEqual(providers["claude"]["tokens"], 300)
        share = report["routing"]["codex_share_of_implementation"]
        self.assertEqual(share["count_share"], 0.5)
        self.assertEqual(share["token_weighted_share"], 0.25)

        fallbacks = report["fallbacks"]
        self.assertEqual(fallbacks["jobs_with_fallback"], 1)
        self.assertEqual(fallbacks["jobs_with_multiple_attempted_models"], 1)
        self.assertEqual(fallbacks["refusals_total"], 2)
        self.assertEqual(fallbacks["jobs_with_effort_downgrade"], 1)
        self.assertEqual(report["outcomes"]["by_effort"]["unknown"]["statuses"], {"failed": 1})
        self.assertEqual(fallbacks["by_effort"]["high"]["fallback_count"], 1)

        text = render(report)
        self.assertTrue(text)
        self.assertIn("routing", text)
        self.assertIn("insufficient data (n=2)", text)

    def test_local_usage_remains_a_distinct_provider_bucket(self):
        with Ledger(self.root) as data:
            data["jobs"].append(
                {
                    "id": "local-author",
                    "ts": iso(self.current - 30),
                    "provider": "local",
                    "route_provider": "local",
                    "kind": "implementation",
                    "model": "qwen3.8-27b",
                    "tokens": 75,
                    "status": "completed",
                }
            )

        report = collect(self.root, self.settings, current=self.current)
        local = report["routing"]["by_provider"]["local"]
        self.assertEqual(local["jobs"], 1)
        self.assertEqual(local["tokens"], 75)
        self.assertIn("local", render(report))

    def test_effort_evidence_preserves_failures(self):
        with Ledger(self.root) as data:
            data["jobs"].extend(
                [
                    {
                        "id": f"medium-{status}",
                        "ts": iso(self.current - 30),
                        "goal_id": "g1" if status == "failed" else "g3",
                        "iteration": 3 if status == "failed" else 1,
                        "provider": "codex",
                        "kind": "review",
                        "effort": "medium",
                        "status": status,
                        "fallback_count": 2 if status == "failed" else 0,
                        "attempted_models": ["a", "b", "c"] if status == "failed" else [],
                        "refused_models": ["a"] if status == "failed" else [],
                        "effort_downgraded_from": "high" if status == "failed" else None,
                    }
                    for status in (
                        "failed",
                        "timeout",
                        "capacity",
                        "cancelled",
                        "blocked_needs_controller",
                    )
                ]
            )

        report = collect(self.root, self.settings, current=self.current)
        medium_outcomes = report["outcomes"]["by_effort"]["medium"]
        self.assertEqual(
            medium_outcomes["statuses"],
            {
                "completed": 1,
                "failed": 1,
                "timeout": 1,
                "capacity": 1,
                "cancelled": 1,
                "blocked_needs_controller": 1,
            },
        )
        evidence = report["fallbacks"]["by_effort"]["medium"]
        self.assertEqual(evidence["jobs"], 6)
        self.assertEqual(evidence["fallback_count"], 2)
        self.assertEqual(evidence["attempted_model_count"], 3)
        self.assertEqual(evidence["refusal_count"], 1)
        self.assertEqual(evidence["effort_downgrade_count"], 1)

        text = render(report)
        self.assertIn("blocked_needs_controller=1", text)
        self.assertIn("unknown", text)

    def test_parser_accepts_stats_options(self):
        args = build_parser().parse_args(["stats", "--days", "7", "--json"])
        self.assertEqual(args.handler, "stats")
        self.assertEqual(args.days, 7.0)
        self.assertTrue(args.as_json)


if __name__ == "__main__":
    unittest.main()
