from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_seats import router
from codex_seats import cli
from codex_seats.bounded_process import ProcessCancelledError
from codex_seats.claude_isolation import (
    _tool_path,
    claude_head_settings,
    claude_trusted_job_settings,
)
from codex_seats.development_policy import (
    DevelopmentPolicyError,
    RESTRICTED_ACCESS,
    TRUSTED_DEVELOPMENT_ACCESS,
    WORKER_ACCESS_ENV_KEY,
    resolve_worker_access,
)
from codex_seats.heads import dispatch_head
from codex_seats.provider_claude import run_claude
from codex_seats.provider_codex import run_codex
from codex_seats.state import (
    Ledger,
    iso,
    load_settings,
    set_worker_access,
)
from codex_seats.worker_hook import worker_decision
from codex_seats.worker_config import (
    sync_codex_capability_roots,
    validate_trusted_codex_config,
)
from codex_seats.workers import RunResult
from registry_fixture import registry_settings, write_settings


def _trusted_settings() -> dict:
    settings = registry_settings()
    settings["execution"]["worker_access"] = TRUSTED_DEVELOPMENT_ACCESS
    return settings


def _ready_codex(root: Path, settings: dict) -> None:
    write_settings(root, settings)
    for seat_id in ("seat1", "seat2", "seat3", "seat4", "seat5"):
        home = root / seat_id
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text("")
    with Ledger(root) as data:
        seat = data["seats"]["seat3"]
        observed = time.time()
        seat.update(
            status="ok",
            pct_left=100,
            probed_at=iso(),
            usage_updated_at=iso(),
            last_probe_limits={"weekly": {"pct": 100, "window_minutes": 10080}},
            active_window_observation={
                "source": "account", "complete": True, "observed_at": observed,
                "observed_windows": ["weekly"], "active_windows": ["weekly"],
            },
        )


class AccessResolutionTests(unittest.TestCase):
    def test_default_invalid_narrowing_and_raw_override(self):
        self.assertEqual(resolve_worker_access(registry_settings()).effective, "restricted")
        settings = _trusted_settings()
        self.assertTrue(resolve_worker_access(settings).trusted)
        self.assertEqual(
            resolve_worker_access(settings, {"sandbox": "read-only"}).effective,
            "restricted",
        )
        settings["execution"]["worker_access"] = "wide"
        with self.assertRaises(DevelopmentPolicyError):
            resolve_worker_access(settings)
        with self.assertRaises(DevelopmentPolicyError):
            resolve_worker_access(_trusted_settings(), {"sandbox": "danger-full-access"})

    def test_inert_old_filesystem_policy_is_accepted(self):
        # The live install carries an inert worker_filesystem_policy with the
        # claude route disabled and no canary; it must resolve normally without
        # granting any retired development-v1 access.
        for mode, expected in (
            (RESTRICTED_ACCESS, RESTRICTED_ACCESS),
            (TRUSTED_DEVELOPMENT_ACCESS, TRUSTED_DEVELOPMENT_ACCESS),
        ):
            settings = registry_settings()
            settings["execution"]["worker_access"] = mode
            settings["execution"]["worker_filesystem_policy"] = {
                "policy": "development-v1",
                "secret_inventory": "explicit-v1",
                "routes": {"claude": {"enabled": False}},
            }
            for enabled in (False, True):
                settings["execution"]["worker_filesystem_policy"]["routes"]["claude"]["enabled"] = enabled
                with self.subTest(mode=mode, retired_route_enabled=enabled):
                    self.assertEqual(resolve_worker_access(settings).effective, expected)

    def test_present_canary_is_refused_as_retired_in_every_mode(self):
        # A leftover routes.*.canary grant must refuse worker execution as
        # retired regardless of the configured access mode, never run as an
        # ordinary trusted job.
        for mode in (RESTRICTED_ACCESS, TRUSTED_DEVELOPMENT_ACCESS):
            settings = registry_settings()
            settings["execution"]["worker_access"] = mode
            settings["execution"]["worker_filesystem_policy"] = {
                "policy": "development-v1",
                "routes": {"claude": {"enabled": False, "canary": {"recipe": "x"}}},
            }
            for request in ({}, {"sandbox": "read-only"}, {"strict_scope": True}, {"parallel_safe": True}):
                with self.subTest(mode=mode, request=request):
                    with self.assertRaisesRegex(DevelopmentPolicyError, "retired"):
                        resolve_worker_access(settings, request)

    def test_retired_development_route_command_is_rejected(self):
        # The development-v1 route enable/disable CLI is removed; argparse
        # rejects the retired subcommand as unknown.
        parser = cli.build_parser()
        for argv in (
            ["development-route", "enable", "--provider-version", "x", "--evidence", "y"],
            ["development-route", "disable"],
        ):
            with self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_settings_transaction_supports_selection_and_rollback(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "settings.json").write_text(json.dumps(registry_settings()))
            set_worker_access(root, TRUSTED_DEVELOPMENT_ACCESS)
            self.assertEqual(
                json.loads((root / "settings.json").read_text())["execution"]["worker_access"],
                TRUSTED_DEVELOPMENT_ACCESS,
            )
            set_worker_access(root, "restricted")
            self.assertEqual(
                json.loads((root / "settings.json").read_text())["execution"]["worker_access"],
                "restricted",
            )

    def test_config_selector_requires_controller_authority_to_enable(self):
        settings = registry_settings()
        parser = cli.build_parser()
        denied = parser.parse_args(
            ["config", "--worker-access", TRUSTED_DEVELOPMENT_ACCESS]
        )
        with mock.patch.object(
            cli, "current_controller",
            return_value=SimpleNamespace(managed_worker=False, is_controller=True),
        ):
            self.assertEqual(cli.cmd_config(denied, settings), 2)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "settings.json").write_text(json.dumps(settings))
            allowed = parser.parse_args([
                "config", "--worker-access", TRUSTED_DEVELOPMENT_ACCESS,
                "--from-current-user-request",
            ])
            write_settings(root, settings)
            with mock.patch.object(cli, "ROOT", root), mock.patch.object(
                cli, "current_controller",
                return_value=SimpleNamespace(managed_worker=False, is_controller=True),
            ):
                self.assertEqual(cli.cmd_config(allowed, settings), 0)
            self.assertEqual(
                json.loads((root / "settings.json").read_text())["execution"]["worker_access"],
                TRUSTED_DEVELOPMENT_ACCESS,
            )


class SnapshotTests(unittest.TestCase):
    def test_router_freezes_access_across_provider_fallback(self):
        settings = _trusted_settings()
        settings["providers"]["claude"]["mode"] = "opportunistic"
        settings["providers"]["claude"]["eligible_kinds"].append("implementation")
        seen = []

        def codex(*args, **kwargs):
            seen.append(kwargs["access_decision"])
            settings["execution"]["worker_access"] = "restricted"
            return RunResult(3, "unavailable", "codex", "-", "job")

        def claude(*args, **kwargs):
            seen.append(kwargs["access_decision"])
            return RunResult(0, "completed", "claude", "claude", "job")

        choose = lambda requested, *a, **k: (  # noqa: E731
            ("claude", None) if requested == "claude" else ("codex", None)
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, mock.patch.object(
            router, "choose_provider", side_effect=choose
        ), mock.patch.object(router, "run_codex", side_effect=codex), mock.patch.object(
            router, "run_claude", side_effect=claude
        ):
            result = router.dispatch_run(
                Path(directory), settings,
                {"provider": "auto", "kind": "implementation", "prompt": "p"},
            )
        self.assertEqual(result.status, "completed", result.message)
        self.assertIs(seen[0], seen[1])
        self.assertTrue(seen[0].trusted)

    def test_dispatch_head_refreshes_settings_each_iteration(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            settings = write_settings(root, registry_settings())
            (root / "settings.json").write_text(json.dumps(settings))
            seen = []

            def fake_dispatch(_root, snapshot, _request):
                seen.append(snapshot["execution"]["worker_access"])
                return RunResult(0, "completed", "codex", "seat3", "job")

            head = dispatch_head(root, settings, dispatch_fn=fake_dispatch)
            head(root, "one")
            updated = registry_settings()
            updated["execution"]["worker_access"] = TRUSTED_DEVELOPMENT_ACCESS
            (root / "settings.json").write_text(json.dumps(updated))
            head(root, "two")
        self.assertEqual(seen, ["restricted", TRUSTED_DEVELOPMENT_ACCESS])


class ProviderConstructionTests(unittest.TestCase):
    def test_codex_trusted_rejects_competing_legacy_sandbox_keys(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            home = Path(directory)
            (home / "config.toml").write_text('sandbox_mode = "workspace-write"\n')
            with self.assertRaisesRegex(ValueError, "competing legacy sandbox"):
                validate_trusted_codex_config(home)

    def test_codex_trusted_uses_builtin_profile_and_native_capabilities(self):
        settings = _trusted_settings()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            _ready_codex(root, settings)
            skill = root / "seat1" / "skills" / "demo"
            (skill / "scripts").mkdir(parents=True)
            (skill / "SKILL.md").write_text("use scripts/check.sh")
            (skill / "scripts" / "check.sh").write_text("#!/bin/sh\n")
            captured = {}

            def execute(command, _output, *_args):
                captured["command"] = list(command)
                captured["env"] = _args[2]
                result_path = Path(command[command.index("--output-last-message") + 1])
                result_path.write_text(json.dumps({
                    "status": "completed", "summary": "ok", "changed_files": [],
                    "test_evidence": [], "warnings": [], "needs_controller": None,
                }))
                return "completed", "", 0, 1

            with mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"), mock.patch(
                "codex_seats.workers.PowerLease", return_value=nullcontext()
            ), mock.patch("codex_seats.workers._execute", side_effect=execute), mock.patch(
                "codex_seats.provider_codex.refresh_passive_usage"
            ), mock.patch(
                "codex_seats.provider_codex.refresh_transients"
            ):
                result = run_codex(root, settings, {
                    "prompt": "p", "seat": 3, "no_probe": True, "cd": str(root),
                })
            self.assertEqual(result.status, "completed", result.message)
            joined = " ".join(captured["command"])
            self.assertIn('default_permissions=":danger-full-access"', joined)
            self.assertNotIn("--strict-config", captured["command"])
            self.assertNotIn("features.apps=false", joined)
            self.assertNotIn("features.plugins=false", joined)
            self.assertEqual(captured["env"][WORKER_ACCESS_ENV_KEY], TRUSTED_DEVELOPMENT_ACCESS)
            self.assertEqual((root / "seat3" / "skills").resolve(), (root / "seat1" / "skills").resolve())
            self.assertTrue((root / "seat3" / "skills" / "demo" / "scripts" / "check.sh").exists())
            self.assertEqual(result.structured_result["worker_access"], TRUSTED_DEVELOPMENT_ACCESS)

    def test_claude_trusted_settings_are_additive_and_unsandboxed(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            doc = claude_trusted_job_settings(root, root / "tmp", root / "hook")
        self.assertEqual(doc["sandbox"], {"enabled": False})
        permissions = doc["permissions"]
        # The invalid broad allow globs are gone; the hook authorizes ordinary
        # tools instead, so no static MCP/tool catalog is enumerated.
        self.assertEqual(permissions["allow"], [])
        self.assertFalse(any("mcp__" in rule for rule in permissions["allow"]))
        # The hook must see every tool (matcher "*") so MCP calls are authorized
        # under dontAsk without a per-server rule.
        matchers = [entry["matcher"] for entry in doc["hooks"]["PreToolUse"]]
        self.assertEqual(matchers, ["*"])
        # No file-tool Write/Glob/Grep path rules (ineffective per Claude docs);
        # only Read/Edit denies plus Bash and the Agent tool ban.
        self.assertFalse(
            any(rule.startswith(("Write(", "Glob(", "Grep(")) for rule in permissions["deny"])
        )
        for key in ("enabledPlugins", "disabledMcpjsonServers", "enableAllProjectMcpServers"):
            self.assertNotIn(key, doc)

    def test_claude_trusted_command_keeps_native_settings_discovery(self):
        settings = _trusted_settings()
        settings["providers"]["claude"]["mode"] = "manual_only"
        captured = {}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)

            def execute(command, output, *_args):
                captured["command"] = list(command)
                captured["settings"] = json.loads(
                    Path(command[command.index("--settings") + 1]).read_text()
                )
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({
                    "type": "result", "subtype": "success", "is_error": False,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "structured_output": {
                        "status": "completed", "summary": "ok", "changed_files": [],
                        "test_evidence": [], "warnings": [], "needs_controller": None,
                    },
                }))
                return "completed", "", 0, 1

            with mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/claude"), mock.patch(
                "codex_seats.workers.PowerLease", return_value=nullcontext()
            ), mock.patch("codex_seats.workers._execute", side_effect=execute), mock.patch(
                "codex_seats.provider_claude.load_claude_rate_limits", return_value={}
            ):
                result = run_claude(root, settings, {
                    "prompt": "p", "provider": "claude", "_capacity_checked": True,
                    "cd": str(root),
                })
        command = captured["command"]
        self.assertEqual(result.status, "completed", result.message)
        self.assertNotIn("--setting-sources", command)
        self.assertNotIn("--strict-mcp-config", command)
        self.assertNotIn("--tools", command)
        self.assertNotIn("--no-chrome", command)
        self.assertEqual(captured["settings"]["sandbox"], {"enabled": False})
        self.assertEqual(result.structured_result["worker_access"], TRUSTED_DEVELOPMENT_ACCESS)


class ProviderCancellationTests(unittest.TestCase):
    def test_cancellation_releases_claim_without_retry_or_fallback(self):
        for provider, run in (("codex", run_codex), ("claude", run_claude)):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = _trusted_settings()
                settings["providers"]["claude"]["mode"] = "manual_only"
                _ready_codex(root, settings)
                with (
                    mock.patch("codex_seats.workers.resolve_executable", return_value=f"/fake/{provider}"),
                    mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                    mock.patch("codex_seats.workers._execute", side_effect=ProcessCancelledError("cancelled")) as execute,
                    mock.patch("codex_seats.provider_codex.refresh_passive_usage"),
                    mock.patch("codex_seats.provider_codex.refresh_transients"),
                    mock.patch("codex_seats.provider_claude.load_claude_rate_limits", return_value={}),
                ):
                    result = run(root, settings, {
                        "prompt": "p", "provider": provider, "_capacity_checked": True,
                        "seat": 3, "no_probe": True, "cd": str(root),
                    })
                self.assertEqual(result.status, "cancelled", result.message)
                self.assertFalse(result.fallback_safe)
                self.assertEqual(execute.call_count, 1)
                with Ledger(root) as data:
                    target = data["seats"]["seat3"] if provider == "codex" else data["providers"]["claude"]
                    self.assertIsNone(target["in_flight"])
                self.assertFalse(any((root / "worker-launches").iterdir()))


class PolicySeparationTests(unittest.TestCase):
    def _env(self, root: Path) -> dict[str, str]:
        return {
            "CODEX_SEATS_MANAGED_WORKER": "1",
            WORKER_ACCESS_ENV_KEY: TRUSTED_DEVELOPMENT_ACCESS,
            "CODEX_SEATS_TASK_ROOT": str(root / "repo"),
            "CODEX_SEATS_CONTROL_ROOT": str(root / "control"),
            "CODEX_SEATS_WRITE_SCOPE": json.dumps([{"path": ".", "type": "tree"}]),
            "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
            "CODEX_SEATS_PROTECTED_PATHS": "[]",
            "TMPDIR": str(root / "scratch"),
            "HOME": str(root / "home"),
        }

    def _allowed(self, decision) -> bool:
        return (
            isinstance(decision, dict)
            and decision.get("hookSpecificOutput", {}).get("permissionDecision") == "allow"
        )

    def _denied(self, decision) -> bool:
        return (
            isinstance(decision, dict)
            and decision.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        )

    def test_trusted_hook_allows_ordinary_tools_and_blocks_protected_actions(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            env = self._env(root)
            # Under dontAsk an undecided ordinary tool would be silently denied,
            # so the trusted hook must actively return allow (including MCP).
            for tool in ("WebSearch", "WebFetch", "Computer", "mcp", "mcp__playwright"):
                self.assertTrue(self._allowed(worker_decision({"tool_name": tool}, env)), tool)
            self.assertTrue(self._allowed(worker_decision({
                "tool_name": "Bash", "tool_input": {"command": "open -a Simulator"},
            }, env)))
            self.assertTrue(self._denied(worker_decision({
                "tool_name": "Bash", "tool_input": {"command": "codex-seat delegate x"},
            }, env)))
            self.assertTrue(self._denied(worker_decision({
                "tool_name": "Bash", "tool_input": {"command": "git push origin main"},
            }, env)))
            self.assertTrue(self._allowed(worker_decision({
                "tool_name": "Write", "tool_input": {"file_path": str(root / "scratch" / "out")},
            }, env)))
            self.assertTrue(self._denied(worker_decision({
                "tool_name": "Write", "tool_input": {"file_path": str(root / "other" / "out")},
            }, env)))
            # The Agent tool stays disabled so a trusted worker cannot delegate.
            self.assertTrue(self._denied(worker_decision({"tool_name": "Agent"}, env)))

    def test_head_policy_does_not_inherit_worker_tool_bans(self):
        doc = claude_head_settings("/control", registry_settings())
        deny = doc["permissions"]["deny"]
        self.assertNotIn("Agent", deny)
        self.assertNotIn("WebSearch", deny)
        self.assertFalse(any("open -a" in rule or "osascript" in rule for rule in deny))


class MalformedSettingsTests(unittest.TestCase):
    def test_unhashable_worker_access_fails_closed_not_typeerror(self):
        # A list/dict value is unhashable; membership must not raise TypeError.
        settings = registry_settings()
        settings["execution"]["worker_access"] = ["trusted-development"]
        with self.assertRaises(DevelopmentPolicyError):
            resolve_worker_access(settings)
        settings["execution"]["worker_access"] = {"mode": "trusted-development"}
        with self.assertRaises(DevelopmentPolicyError):
            resolve_worker_access(settings)

    def test_load_settings_recovers_from_unhashable_worker_access(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            document = registry_settings()
            document["execution"]["worker_access"] = ["trusted-development"]
            (root / "settings.json").write_text(json.dumps(document))
            merged, warnings = load_settings(root)
            # Fail closed to the restricted default with a surfaced warning
            # instead of crashing on an uncaught TypeError.
            self.assertEqual(merged["execution"]["worker_access"], "restricted")
            self.assertTrue(any("worker_access" in warning for warning in warnings))

    def test_dispatch_head_surfaces_reload_rejection(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "settings.json").write_text(json.dumps(registry_settings()))

            def fake_dispatch(*_args, **_kwargs):
                raise AssertionError("must not dispatch when reload is rejected")

            head = dispatch_head(root, registry_settings(), dispatch_fn=fake_dispatch)
            # Corrupt the on-disk document after building the head so the next
            # iteration's reload validation fails.
            (root / "settings.json").write_text("{ not json")
            result = head(root, "one")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked_needs_controller")
        self.assertIn("settings reload rejected", result.summary)


class CapabilityRootTests(unittest.TestCase):
    def _env(self, control: Path, homes: list[str]) -> dict[str, str]:
        return {
            "CODEX_SEATS_MANAGED_WORKER": "1",
            WORKER_ACCESS_ENV_KEY: TRUSTED_DEVELOPMENT_ACCESS,
            "CODEX_SEATS_CONTROL_ROOT": str(control),
            "CODEX_SEATS_PROTECTED_HOMES": json.dumps(homes),
            "CODEX_SEATS_WRITE_SCOPE": json.dumps([{"path": ".", "type": "tree"}]),
            "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
            "CODEX_SEATS_PROTECTED_PATHS": "[]",
        }

    def _allowed(self, decision) -> bool:
        return (
            decision is None
            or decision.get("hookSpecificOutput", {}).get("permissionDecision") == "allow"
        )

    def _read(self, path: Path, env: dict[str, str]):
        return worker_decision(
            {"tool_name": "Read", "tool_input": {"file_path": str(path)}}, env
        )

    def test_capability_read_covers_default_and_custom_homes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            control = root / "control"
            custom = root / "elsewhere" / "customhome"
            env = self._env(control, [str(control / "seat*"), str(custom)])
            # Default pool-relative seat home skill resource.
            self.assertTrue(
                self._allowed(self._read(control / "seat3" / "skills" / "demo" / "SKILL.md", env))
            )
            # Custom seat home outside the control root, plugins subtree.
            self.assertTrue(
                self._allowed(self._read(custom / "plugins" / "x" / "manifest.json", env))
            )

    def test_capability_carveout_still_protects_credentials_and_controls(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            control = root / "control"
            custom = root / "customhome"
            env = self._env(control, [str(control / "seat*"), str(custom)])
            for target in (
                control / "seat3" / "config.toml",
                control / "seat3" / "auth.json",
                custom / "auth.json",
                control / "settings.json",
            ):
                decision = self._read(target, env)
                self.assertIsNotNone(decision, target)
                self.assertEqual(
                    decision["hookSpecificOutput"]["permissionDecision"], "deny", target
                )


class CapabilitySyncTests(unittest.TestCase):
    def test_complete_skill_tree_is_shared_by_link(self):
        settings = registry_settings()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "seat1").mkdir()
            (root / "seat3").mkdir()
            skill = root / "seat1" / "skills" / "demo"
            (skill / "scripts").mkdir(parents=True)
            (skill / "SKILL.md").write_text("use scripts/run.sh")
            (skill / "scripts" / "run.sh").write_text("#!/bin/sh\n")
            warnings = sync_codex_capability_roots(root, settings, "seat3")
            self.assertEqual(warnings, [])
            # The whole referenced tree — instruction plus scripts — is reachable.
            self.assertEqual(
                (root / "seat3" / "skills").resolve(), (root / "seat1" / "skills").resolve()
            )
            self.assertTrue((root / "seat3" / "skills" / "demo" / "scripts" / "run.sh").exists())

    def test_explicit_seat_local_tree_is_preserved_and_reported(self):
        settings = registry_settings()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "seat1" / "skills").mkdir(parents=True)
            (root / "seat1" / "skills" / "SHARED.md").write_text("primary")
            # A pre-existing seat-local skills tree is an explicit local choice.
            (root / "seat3" / "skills").mkdir(parents=True)
            (root / "seat3" / "skills" / "LOCAL.md").write_text("local")
            warnings = sync_codex_capability_roots(root, settings, "seat3")
            # Not overwritten, and the discovery difference is surfaced.
            self.assertTrue((root / "seat3" / "skills" / "LOCAL.md").exists())
            self.assertFalse((root / "seat3" / "skills" / "SHARED.md").exists())
            self.assertTrue(any("seat-local" in warning for warning in warnings))


class TrustedSettingsShapeTests(unittest.TestCase):
    def test_head_control_denies_use_absolute_double_slash(self):
        doc = claude_head_settings("/control", registry_settings())
        control_rules = [
            rule
            for rule in doc["permissions"]["deny"]
            if rule.startswith(("Read(/", "Edit(/"))
        ]
        self.assertTrue(control_rules)
        # Absolute file-tool paths need the doubled leading slash; a single one
        # is interpreted as project-relative and silently fails to protect.
        for rule in control_rules:
            inner = rule[rule.index("(") + 1 : -1]
            self.assertTrue(inner.startswith("//"), rule)
        # No ineffective Write/Glob/Grep file-tool rules in the head deny list.
        self.assertFalse(
            any(rule.startswith(("Write(", "Glob(", "Grep(")) for rule in doc["permissions"]["deny"])
        )

    def test_trusted_settings_keep_capability_roots_readable(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            control = (Path(directory) / "control").resolve()
            settings = registry_settings()
            doc = claude_trusted_job_settings(
                control / "repo", control / "tmp", control / "hook",
                control_root=control, settings=settings,
            )
        deny = doc["permissions"]["deny"]
        seat_home = control / "seat*"
        seat_exact = _tool_path(seat_home)
        seat_tree = _tool_path(seat_home, tree=True)
        # Seat homes are closed to edits but never blanket read-denied, so native
        # skill/plugin discovery keeps its complete resource trees.
        self.assertIn(f"Edit({seat_tree})", deny)
        self.assertNotIn(f"Read({seat_exact})", deny)
        self.assertNotIn(f"Read({seat_tree})", deny)
        # Credentials are still denied by a valid Read glob anywhere.
        self.assertIn("Read(**/auth.json)", deny)


if __name__ == "__main__":
    unittest.main()
