"""R1: symmetric Codex<->Claude fallback in dispatch_run.

The router already fell Claude->Codex on `fallback_safe`; these cover the new
mirror direction (Codex pool exhausted -> Claude) plus its mode guard, so the
provider swap the dev-loop promises is actually implemented and honored."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_seats import router
from codex_seats.state import Ledger, default_settings
from codex_seats.workers import RunResult


def _opportunistic_settings() -> dict:
    s = default_settings()
    s["providers"]["claude"]["mode"] = "opportunistic"
    kinds = s["providers"]["claude"].setdefault("eligible_kinds", [])
    if "implementation" not in kinds:
        kinds.append("implementation")
    return s


def _exhausted(status: str = "unavailable") -> RunResult:
    # worker="-" is the "no schedulable seat" signal (RunResult's seat field is `worker`).
    return RunResult(3, status, "codex", "-", "job")


def _schema3_settings(*, claude_mode: str = "off") -> dict:
    settings = default_settings()
    settings["pool"]["seats"] = {
        "seat1": {
            "label": "rolling-a",
            "lifecycle": "active",
            "home": None,
            "capacity_profile": "personal-plus",
            "relative_capacity": 1.0,
            "capacity_source": "fixture",
            "capacity_verified_at": "2026-09-20T00:00:00Z",
        },
        "seat2": {
            "label": "rolling-b",
            "lifecycle": "active",
            "home": None,
            "capacity_profile": "business-standard",
            "relative_capacity": 1.0,
            "capacity_source": "fixture",
            "capacity_verified_at": "2026-09-20T00:00:00Z",
        },
        "seat3": {
            "label": "premium",
            "lifecycle": "active",
            "home": None,
            "capacity_profile": "business-premium",
            "relative_capacity": 5.0,
            "capacity_source": "fixture",
            "capacity_verified_at": "2026-09-20T00:00:00Z",
        },
    }
    settings["pool"]["primary"] = "seat1"
    settings["providers"]["claude"]["mode"] = claude_mode
    return settings


def _local_settings(*, mode: str = "opportunistic") -> dict:
    settings = _schema3_settings()
    settings["providers"]["local"].update(mode=mode, repair_budget=2)
    settings["local_hosts"] = {
        "local-a": {
            "label": "Studio",
            "lifecycle": "active",
            "resource_groups": {"gpu": {"max_concurrency": 1}},
            "base_url": "http://local-a:8000/v1",
            "availability_probe": {"argv": ["/usr/bin/true"], "timeout_seconds": 1},
        }
    }
    settings["local_seats"] = {
        "local-a-qwen": {
            "label": "Studio Qwen",
            "lifecycle": "active",
            "mode": mode,
            "host": "local-a",
            "resource_group": "gpu",
            "model_profile": "qwen3-8-27b",
            "harness": "qwen",
            "capabilities": {
                "task_shapes": ["execute", "investigate"],
                "validation": ["strong"],
            },
            "max_concurrency": 1,
            "runtime_fingerprint": "sha256:" + "a" * 64,
        }
    }
    return settings


def _local_ready(
    settings: dict,
    host_id: str = "local-a",
    model_profile: str = "qwen3-8-27b",
) -> dict:
    host = settings["local_hosts"][host_id]
    return {
        "host": host_id,
        "state": "ready",
        "reason": "ready",
        "observed_at": __import__("time").time(),
        "config_digest": router.local_seats._host_digest(host),
        "model_profile": model_profile,
    }


class AutoClaudePermittedTests(unittest.TestCase):
    def test_off_and_manual_only_forbid_auto_claude(self):
        s = default_settings()
        for mode in ("off", "manual_only"):
            s["providers"]["claude"]["mode"] = mode
            self.assertFalse(router._auto_claude_permitted(s, "implementation"), mode)

    def test_review_only_permits_only_review_kinds(self):
        s = default_settings()
        s["providers"]["claude"]["mode"] = "review_only"
        s["providers"]["claude"]["eligible_kinds"] = ["implementation", "review"]
        self.assertTrue(router._auto_claude_permitted(s, "review"))
        self.assertFalse(router._auto_claude_permitted(s, "implementation"))

    def test_opportunistic_permits_eligible_kinds(self):
        self.assertTrue(router._auto_claude_permitted(_opportunistic_settings(), "implementation"))


class PreferredProviderTests(unittest.TestCase):
    def test_build_kinds_prefer_codex_review_kinds_prefer_claude(self):
        s = default_settings()
        for kind in ("implementation", "mechanical", "test", "docs", "debug"):
            self.assertEqual(router.preferred_provider(s, kind), "codex", kind)
        for kind in ("review", "audit", "security", "planning"):
            self.assertEqual(router.preferred_provider(s, kind), "claude", kind)

    def test_unknown_kind_defaults_to_codex_and_config_is_tunable(self):
        s = default_settings()
        self.assertEqual(router.preferred_provider(s, "totally-new-kind"), "codex")
        s["execution"]["prefer_provider_by_kind"]["implementation"] = "claude"
        self.assertEqual(router.preferred_provider(s, "implementation"), "claude")


class CodexPoolExhaustedTests(unittest.TestCase):
    def test_no_seat_results_are_exhausted(self):
        self.assertTrue(router._codex_pool_exhausted(_exhausted("unavailable")))
        self.assertTrue(router._codex_pool_exhausted(_exhausted("queued")))

    def test_a_real_or_reserved_run_is_not_exhausted(self):
        self.assertFalse(router._codex_pool_exhausted(RunResult(0, "completed", "codex", "seat1", "job")))
        self.assertFalse(router._codex_pool_exhausted(RunResult(3, "reserved", "codex", "seat2", "job")))


class DispatchCodexToClaudeFallbackTests(unittest.TestCase):
    def _run(self, settings, choose):
        claude_ok = RunResult(0, "completed", "claude", "claude", "job")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(router, "choose_provider", side_effect=choose), \
                mock.patch.object(router, "run_codex", return_value=_exhausted()) as codex, \
                mock.patch.object(router, "run_claude", return_value=claude_ok) as claude:
            result = router.dispatch_run(
                Path(d), settings, {"provider": "auto", "kind": "implementation", "prompt": "do x"}
            )
        return result, codex, claude

    def test_auto_falls_to_claude_when_pool_exhausted(self):
        # auto initially routes to codex; the codex pool is exhausted; Claude is a
        # viable alternative -> the run completes on Claude. no_queue remains an
        # internal ownership marker, though providers never enqueue either way.
        choose = lambda requested, *a, **k: ("claude", None) if requested == "claude" else ("codex", None)  # noqa: E731
        result, codex, claude = self._run(_opportunistic_settings(), choose)
        self.assertEqual(result.provider, "claude")
        self.assertTrue(claude.called)
        self.assertEqual(codex.call_count, 1)  # only the no-queue probe; no second self-queue call
        self.assertIs(codex.call_args_list[0].args[2].get("no_queue"), True)
        self.assertIn("fell back to Claude", result.message or "")

    def test_manual_only_never_auto_falls_to_claude(self):
        # Policy guard: manual_only forbids auto-Claude, so capacity returns to the
        # caller rather than smuggling the task onto Claude.
        s = default_settings()
        s["providers"]["claude"]["mode"] = "manual_only"
        choose = lambda requested, *a, **k: ("codex", None)  # noqa: E731
        result, _codex, claude = self._run(s, choose)
        self.assertFalse(claude.called)          # policy respected: no auto-Claude
        self.assertEqual(result.status, "unavailable")


class RoutePersistenceTests(unittest.TestCase):
    """The router's decision must land in the request so the job ledger records
    routing even for non-broker dispatches — without ever clobbering a
    broker-supplied route_reason."""

    def _dispatch(self, settings, request, provider_choice="codex"):
        ok = RunResult(0, "completed", provider_choice, provider_choice, "job")
        choose = lambda requested, *a, **k: (provider_choice, None)  # noqa: E731
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(router, "choose_provider", side_effect=choose), \
                mock.patch.object(router, "run_codex", return_value=ok) as codex, \
                mock.patch.object(router, "run_claude", return_value=ok) as claude:
            router.dispatch_run(Path(d), settings, request)
        calls = codex.call_args_list + claude.call_args_list
        return calls[0].args[2]  # the request the provider actually received

    def test_router_decision_is_persisted(self):
        seen = self._dispatch(
            _opportunistic_settings(),
            {"provider": "auto", "kind": "implementation", "prompt": "do x"},
        )
        self.assertEqual(seen["route_provider"], "codex")
        self.assertEqual(seen["route_reason"], "router:auto->codex")

    def test_broker_route_reason_is_never_clobbered(self):
        seen = self._dispatch(
            _opportunistic_settings(),
            {
                "provider": "auto", "kind": "implementation", "prompt": "do x",
                "route_reason": "campaign ROI admission",
            },
        )
        self.assertEqual(seen["route_provider"], "codex")
        self.assertEqual(seen["route_reason"], "campaign ROI admission")

    def test_hard_off_records_the_forced_route(self):
        s = default_settings()  # claude mode defaults to off
        ok = RunResult(0, "completed", "codex", "seat1", "job")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(router, "run_codex", return_value=ok) as codex:
            router.dispatch_run(
                Path(d), s, {"provider": "auto", "kind": "implementation", "prompt": "p"}
            )
        seen = codex.call_args_list[0].args[2]
        self.assertEqual(seen["route_provider"], "codex")
        self.assertEqual(seen["route_reason"], "router:claude-hard-off")
        self.assertEqual(seen["model"], "gpt-6-sol")

    def test_hard_off_does_not_disable_an_explicit_codex_request(self):
        settings = default_settings()
        completed = RunResult(0, "completed", "codex", "seat1", "job")
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(router, "run_codex", return_value=completed) as codex:
            result = router.dispatch_run(
                Path(directory),
                settings,
                {"provider": "codex", "kind": "implementation", "prompt": "p"},
            )
        self.assertEqual(result.status, "completed")
        self.assertTrue(codex.called)


class WorkerMcpRoutingTests(unittest.TestCase):
    def test_auto_mcp_request_respects_claude_hard_off(self):
        settings = default_settings()
        completed = RunResult(0, "completed", "codex", "seat2", "job")
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(router, "run_codex", return_value=completed) as codex, \
                mock.patch.object(router, "run_claude") as claude:
            result = router.dispatch_run(
                Path(directory),
                settings,
                {
                    "provider": "auto",
                    "kind": "implementation",
                    "prompt": "use it",
                    "mcp": ["visual"],
                },
            )
        self.assertEqual(result.provider, "codex")
        self.assertTrue(codex.called)
        self.assertFalse(claude.called)
        self.assertEqual(codex.call_args.args[2]["provider"], "codex")
        self.assertEqual(codex.call_args.args[2]["mcp"], ["visual"])

    def test_explicit_claude_mcp_request_reaches_claude(self):
        completed = RunResult(0, "completed", "claude", "claude", "job")
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(router, "choose_provider", return_value=("claude", None)), \
                mock.patch.object(router, "run_codex") as codex, \
                mock.patch.object(router, "run_claude", return_value=completed) as claude:
            result = router.dispatch_run(
                Path(directory),
                _opportunistic_settings(),
                {
                    "provider": "claude",
                    "kind": "implementation",
                    "prompt": "use it",
                    "mcp": ["visual"],
                },
            )
        self.assertEqual(result.status, "completed")
        self.assertFalse(codex.called)
        self.assertEqual(claude.call_args.args[2]["provider"], "claude")
        self.assertEqual(claude.call_args.args[2]["mcp"], ["visual"])

    def test_auto_mcp_request_can_fall_back_to_claude(self):
        completed = RunResult(0, "completed", "claude", "claude", "job")
        choose = lambda requested, *a, **k: ("claude", None) if requested == "claude" else ("codex", None)  # noqa: E731
        request = {"provider": "auto", "kind": "implementation", "prompt": "use it", "mcp": ["visual"]}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(router, "choose_provider", side_effect=choose), \
                mock.patch.object(router, "run_codex", return_value=_exhausted()) as codex, \
                mock.patch.object(router, "run_claude", return_value=completed) as claude:
            result = router.dispatch_run(Path(directory), _opportunistic_settings(), request)
        self.assertEqual(result.provider, "claude")
        self.assertEqual(codex.call_count, 1)
        self.assertEqual(claude.call_args.args[2]["mcp"], ["visual"])
        self.assertEqual(request["provider"], "auto")


class InteractiveHeadAuthoringRouteTests(unittest.TestCase):
    def test_codex_head_burns_one_x_then_opposing_claude_family(self):
        settings = _schema3_settings(claude_mode="opportunistic")
        completed = RunResult(0, "completed", "claude", "claude", "job")
        request = {
            "provider": "auto",
            "kind": "implementation",
            "prompt": "build it",
            "_origin_head": "codex",
            "_origin_seat": "seat1",
            "_excluded_seats": ["seat1"],
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(router, "load_claude_rate_limits", return_value={}), \
                mock.patch.object(router, "choose_provider", return_value=("claude", None)), \
                mock.patch.object(router, "run_codex", return_value=_exhausted()) as codex, \
                mock.patch.object(router, "run_claude", return_value=completed) as claude:
            result = router.dispatch_run(Path(directory), settings, request)

        self.assertEqual(result.provider, "claude")
        self.assertEqual(codex.call_count, 1)
        first = codex.call_args.args[2]
        self.assertEqual(first["_excluded_seats"], ["seat1", "seat3"])
        self.assertEqual(first["model"], "gpt-6-sol")
        self.assertEqual(first["route_reason"], "router:phase1-codex-1x")
        self.assertNotIn("model", claude.call_args.args[2])
        self.assertEqual(
            claude.call_args.args[2]["route_reason"],
            "router:phase2-opposite-head-family",
        )

    def test_claude_head_burns_one_x_then_uses_opposing_codex_pool_with_astra(self):
        settings = _schema3_settings()
        completed = RunResult(0, "completed", "codex", "seat3", "job")
        request = {
            "provider": "auto",
            "kind": "ui_ux",
            "prompt": "polish the flow",
            "_origin_head": "claude",
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    router, "run_codex", side_effect=[_exhausted(), completed]
                ) as codex, \
                mock.patch.object(router, "run_claude") as claude:
            result = router.dispatch_run(Path(directory), settings, request)

        self.assertEqual(result.worker, "seat3")
        self.assertEqual(codex.call_count, 2)
        first, second = [call.args[2] for call in codex.call_args_list]
        # Phase 1 spends 1x workers only: the 5x seat and the primary wait.
        self.assertEqual(first["_excluded_seats"], ["seat1", "seat3"])
        self.assertEqual(second["route_reason"], "router:phase2-opposite-head-family")
        self.assertEqual(second["model"], "gpt-6-astra")
        self.assertFalse(claude.called)

    def test_codex_head_identity_is_last_resort_isolated_worker(self):
        settings = _schema3_settings()
        completed = RunResult(0, "completed", "codex", "seat1", "job")
        request = {
            "provider": "auto",
            "kind": "docs",
            "prompt": "update the guide",
            "_origin_head": "codex",
            "_origin_seat": "seat1",
            "_excluded_seats": ["seat1"],
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    router,
                    "run_codex",
                    side_effect=[_exhausted(), _exhausted(), completed],
                ) as codex, \
                mock.patch.object(router, "run_claude") as claude:
            result = router.dispatch_run(Path(directory), settings, request)

        self.assertEqual(result.worker, "seat1")
        self.assertEqual(codex.call_count, 3)
        last = codex.call_args_list[-1].args[2]
        self.assertEqual(last["seat"], "seat1")
        self.assertEqual(last["_excluded_seats"], [])
        self.assertEqual(last["model"], "gpt-5.6-terra")
        self.assertEqual(last["route_reason"], "router:phase4-codex-head-worker")
        self.assertFalse(claude.called)


class LocalPhaseZeroRoutingTests(unittest.TestCase):
    def test_ready_local_seat_precedes_every_frontier_provider(self):
        settings = _local_settings()
        completed = RunResult(0, "completed", "local", "local-a-qwen", "job")
        request = {
            "provider": "auto",
            "kind": "implementation",
            "prompt": "build it",
            "task_id": "logical-1",
            "attempt_id": "attempt-1",
            "validation": "strong",
            "_origin_head": "codex",
            "_origin_seat": "seat1",
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    router.local_seats,
                    "observe_host",
                    return_value=_local_ready(settings),
                ) as probe, \
                mock.patch.object(router, "run_local", return_value=completed) as local, \
                mock.patch.object(router, "run_codex") as codex, \
                mock.patch.object(router, "run_claude") as claude:
            result = router.dispatch_run(Path(directory), settings, request)

        self.assertEqual(result.provider, "local")
        self.assertTrue(probe.called)
        self.assertFalse(codex.called or claude.called)
        bound = local.call_args.args[2]
        self.assertEqual(bound["seat"], "local-a-qwen")
        self.assertEqual(bound["route_reason"], "router:phase0-local-ready")
        self.assertEqual(bound["review_cycle"], 0)

    def test_prestart_failure_tries_next_local_host_before_frontier(self):
        settings = _local_settings()
        settings["local_hosts"]["spare"] = {
            "label": "Spare",
            "lifecycle": "active",
            "resource_groups": {"gpu": {"max_concurrency": 1}},
            "base_url": "http://spare:8000/v1",
            "availability_probe": {
                "argv": ["/usr/bin/true"],
                "timeout_seconds": 1,
            },
        }
        settings["local_seats"]["spare-qwen"] = {
            **settings["local_seats"]["local-a-qwen"],
            "label": "Spare Qwen",
            "host": "spare",
            "runtime_fingerprint": "sha256:" + "b" * 64,
        }
        unavailable = RunResult(
            3,
            "unavailable",
            "local",
            "local-a-qwen",
            "job",
            fallback_safe=True,
        )
        completed = RunResult(0, "completed", "local", "spare-qwen", "job")
        request = {
            "provider": "auto",
            "kind": "implementation",
            "prompt": "build it",
            "task_id": "logical-multi-host",
            "attempt_id": "attempt-multi-host",
            "validation": "strong",
            "_origin_head": "codex",
            "_origin_seat": "seat1",
        }

        def ready(_root, _settings, host_id):
            return _local_ready(settings, host_id)

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    router.local_seats, "observe_host", side_effect=ready
                ), \
                mock.patch.object(
                    router, "run_local", side_effect=[unavailable, completed]
                ) as local, \
                mock.patch.object(router, "run_codex") as codex:
            result = router.dispatch_run(Path(directory), settings, request)

        self.assertEqual(result.worker, "spare-qwen")
        self.assertEqual(
            [call.args[2]["seat"] for call in local.call_args_list],
            ["local-a-qwen", "spare-qwen"],
        )
        self.assertEqual(local.call_args_list[1].args[2]["_fallback_count"], 1)
        self.assertFalse(codex.called)

    def test_gaming_local_host_falls_through_without_waiting(self):
        settings = _local_settings()
        gaming = _local_ready(settings) | {"state": "gaming", "reason": "gaming"}
        completed = RunResult(0, "completed", "codex", "seat2", "job")
        request = {
            "provider": "auto",
            "kind": "implementation",
            "prompt": "build it",
            "task_id": "logical-2",
            "attempt_id": "attempt-2",
            "_origin_head": "codex",
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(router.local_seats, "observe_host", return_value=gaming), \
                mock.patch.object(router, "run_local") as local, \
                mock.patch.object(router, "run_codex", return_value=completed) as codex:
            result = router.dispatch_run(Path(directory), settings, request)

        self.assertEqual(result.provider, "codex")
        self.assertFalse(local.called)
        self.assertEqual(codex.call_count, 1)

    def test_canary_is_limited_to_small_routine_work(self):
        settings = _local_settings(mode="canary")
        completed = RunResult(0, "completed", "codex", "seat2", "job")
        request = {
            "provider": "auto",
            "kind": "implementation",
            "prompt": "build it",
            "task_id": "logical-3",
            "_origin_head": "codex",
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(router.local_seats, "observe_host") as probe, \
                mock.patch.object(router, "run_local") as local, \
                mock.patch.object(router, "run_codex", return_value=completed):
            result = router.dispatch_run(Path(directory), settings, request)

        self.assertEqual(result.provider, "codex")
        self.assertFalse(probe.called or local.called)

    def test_repair_budget_and_preempted_attempt_force_frontier(self):
        settings = _local_settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Ledger(root) as data:
                data["jobs"].extend(
                    [
                        {
                            "provider": "local",
                            "task_id": "repair-task",
                            "attempt_id": f"old-{index}",
                            "status": "completed",
                        }
                        for index in range(2)
                    ]
                )
                data["jobs"].append(
                    {
                        "provider": "local",
                        "task_id": "preempt-task",
                        "attempt_id": "same-attempt",
                        "status": "preempted",
                    }
                )
            policy = router.resolve_task_policy(
                settings, "implementation", validation="strong"
            )
            repair = router._select_local_seat(
                root,
                settings,
                {"task_id": "repair-task", "attempt_id": "next"},
                policy,
                automatic=True,
            )
            preempted = router._select_local_seat(
                root,
                settings,
                {"task_id": "preempt-task", "attempt_id": "same-attempt"},
                policy,
                automatic=True,
            )
            preempted_without_task_id = router._select_local_seat(
                root,
                settings,
                {"attempt_id": "same-attempt"},
                policy,
                automatic=True,
            )
        self.assertEqual(repair[2], "repair_budget_exhausted")
        self.assertEqual(preempted[2], "attempt_preempted_by_host")
        self.assertEqual(
            preempted_without_task_id[2], "attempt_preempted_by_host"
        )

    def test_explicit_local_provider_selects_a_seat(self):
        settings = _local_settings()
        completed = RunResult(0, "completed", "local", "local-a-qwen", "job")
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    router.local_seats,
                    "observe_host",
                    return_value=_local_ready(settings),
                ), \
                mock.patch.object(router, "run_local", return_value=completed) as local:
            result = router.dispatch_run(
                Path(directory),
                settings,
                {
                    "provider": "local",
                    "kind": "docs",
                    "prompt": "write it",
                    "task_id": "explicit-1",
                    "validation": "strong",
                },
            )
        self.assertEqual(result.provider, "local")
        self.assertEqual(local.call_args.args[2]["route_reason"], "router:explicit-local")

    def test_explicit_local_provider_with_empty_registry_is_unavailable(self):
        settings = _local_settings()
        settings["local_seats"] = {}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(router, "run_local") as local:
            result = router.dispatch_run(
                Path(directory),
                settings,
                {
                    "provider": "local",
                    "kind": "docs",
                    "prompt": "write it",
                    "task_id": "explicit-empty",
                },
            )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("no_local_seats", result.message)
        self.assertFalse(local.called)

    def test_explicit_local_preemption_is_terminal_for_the_attempt(self):
        settings = _local_settings()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Ledger(root) as data:
                data["jobs"].append(
                    {
                        "provider": "local",
                        "task_id": "explicit-preempt",
                        "attempt_id": "attempt-preempt",
                        "status": "preempted",
                    }
                )
            with mock.patch.object(router, "run_local") as local:
                result = router.dispatch_run(
                    root,
                    settings,
                    {
                        "provider": "local",
                        "kind": "docs",
                        "prompt": "write it",
                        "task_id": "explicit-preempt",
                        "attempt_id": "attempt-preempt",
                        "validation": "strong",
                    },
                )
        self.assertEqual(result.status, "failed")
        self.assertIn("refusing to re-run", result.message)
        self.assertFalse(local.called)

if __name__ == "__main__":
    unittest.main()
