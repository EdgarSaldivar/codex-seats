from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_seats.claude_isolation import (
    DENIED_TOOLS,
    claude_job_settings,
    empty_mcp_config,
    result_schema_json,
    scrubbed_claude_env,
)
from codex_seats.cli import cmd_provider_set
from codex_seats.state import Ledger, default_settings
from codex_seats.telemetry import classify_job_telemetry, validate_job_tokens
from codex_seats.router import choose_provider
from codex_seats.provider_claude import run_claude


class ClaudeModeAndIsolationTests(unittest.TestCase):
    def limits(self) -> dict:
        return {
            "updated_at": time.time(),
            "five_hour": {"remaining_percentage": 80, "resets_at": time.time() + 1_000},
            "seven_day": {"remaining_percentage": 80, "resets_at": time.time() + 10_000},
        }

    def test_modes_and_explicit_small_canary_fail_closed(self):
        settings = default_settings()
        with tempfile.TemporaryDirectory() as directory:
            with Ledger(Path(directory)) as data:
                settings["providers"]["claude"]["mode"] = "manual_only"
                self.assertEqual(
                    choose_provider("auto", "review", settings, data, claude_limits=self.limits())[0],
                    "codex",
                )
                self.assertEqual(
                    choose_provider("claude", "review", settings, data, claude_limits=self.limits())[0],
                    "claude",
                )
                self.assertEqual(
                    choose_provider(
                        "claude", "review", settings, data, size="small",
                        claude_limits={}, small_canary=True,
                    )[0],
                    "claude",
                )
                self.assertEqual(
                    choose_provider(
                        "claude", "review", settings, data, size="small", claude_limits={}
                    )[0],
                    "unavailable",
                )
                self.assertEqual(
                    choose_provider(
                        "claude", "audit", settings, data, size="big",
                        claude_limits={}, allow_reserve=True,
                    )[0],
                    "unavailable",
                )
                settings["providers"]["claude"]["mode"] = "review_only"
                self.assertEqual(
                    choose_provider("auto", "audit", settings, data, claude_limits=self.limits())[0],
                    "claude",
                )
                self.assertEqual(
                    choose_provider(
                        "auto", "implementation", settings, data,
                        size="big", claude_limits=self.limits(),
                    )[0],
                    "codex",
                )

    def test_provider_activation_is_open_to_agents_but_not_managed_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                provider_name="claude",
                mode="manual_only",
                from_current_user_request=False,
                expires_at=None,
                reset_state=False,
                cancel_active=False,
            )
            with (
                mock.patch("codex_seats.cli.ROOT", root),
                mock.patch.dict(
                    os.environ, {"CODEX_SEATS_MANAGED_WORKER": "0"}, clear=True
                ),
                mock.patch("sys.stdin.isatty", return_value=True),
            ):
                # no explicit request flag -> accepted (the flag is optional now)
                self.assertEqual(cmd_provider_set(args, default_settings()), 0)
                # the legacy flag is still accepted
                args.from_current_user_request = True
                self.assertEqual(cmd_provider_set(args, default_settings()), 0)
            settings = json.loads((root / "settings.json").read_text())
            self.assertEqual(settings["providers"]["claude"]["mode"], "manual_only")
            with Ledger(root) as data:
                data["pending_queue"] = [{"prompt": "historical", "provider": "claude"}]
                data["disabled_queue"] = [{"prompt": "older"}]
            with (
                mock.patch("codex_seats.cli.ROOT", root),
                mock.patch.dict(
                    os.environ, {"CODEX_SEATS_MANAGED_WORKER": "0"}, clear=True
                ),
                mock.patch("sys.stdin.isatty", return_value=True),
            ):
                args.mode = "off"
                self.assertEqual(cmd_provider_set(args, default_settings()), 0)
            with Ledger(root) as data:
                self.assertEqual(
                    data["pending_queue"],
                    [{"prompt": "historical", "provider": "claude"}],
                )
                self.assertEqual(data["disabled_queue"], [{"prompt": "older"}])
            # a non-interactive agent shell may enable it; a managed worker may not
            with (
                mock.patch("codex_seats.cli.ROOT", root),
                mock.patch.dict(
                    os.environ, {"CODEX_SEATS_MANAGED_WORKER": "0"}, clear=True
                ),
                mock.patch("sys.stdin.isatty", return_value=False),
            ):
                args.mode = "opportunistic"
                self.assertEqual(cmd_provider_set(args, default_settings()), 0)
            for worker in ({"CODEX_SEATS_MANAGED_WORKER": "1"}, {"CLAUDE_LEAD_WORKER": "1"}):
                with (
                    mock.patch("codex_seats.cli.ROOT", root),
                    mock.patch.dict(os.environ, worker, clear=True),
                ):
                    args.mode = "manual_only"
                    self.assertEqual(cmd_provider_set(args, default_settings()), 2)
            settings = json.loads((root / "settings.json").read_text())
            self.assertEqual(settings["providers"]["claude"]["mode"], "opportunistic")

    def test_settings_and_scrub_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            scope = repo / "src"
            scope.mkdir(parents=True)
            target = scope / "a.txt"
            target.write_text("one")
            private = root / "private"
            private.mkdir()
            hook = root / "worker-hook"
            hook.write_text("")
            settings = claude_job_settings(
                repo,
                [{"path": "src", "type": "tree"}],
                [],
                private,
                hook,
                protected_paths=["src/a.txt"],
            )
            self.assertEqual(settings["permissions"]["defaultMode"], "dontAsk")
            self.assertTrue(settings["sandbox"]["failIfUnavailable"])
            self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
            self.assertEqual(settings["enabledPlugins"], {})
            self.assertEqual(settings["enabledMcpjsonServers"], [])
            self.assertEqual(settings["disabledMcpjsonServers"], ["*"])
            self.assertEqual(empty_mcp_config(), {"mcpServers": {}})
            self.assertEqual(settings["permissions"]["additionalDirectories"], [])
            self.assertEqual(DENIED_TOOLS, ("Agent", "WebFetch", "WebSearch"))
            self.assertNotIn(chr(36) + "schema", json.loads(result_schema_json()))
            self.assertEqual(len(settings["hooks"]["PreToolUse"]), 1)
            self.assertIn(f"Read(/{repo}/**)", settings["permissions"]["allow"])
            self.assertIn(f"Edit(/{scope}/**)", settings["permissions"]["allow"])
            filesystem = settings["sandbox"]["filesystem"]
            self.assertEqual(set(filesystem), {"allowWrite", "denyWrite", "denyRead"})
            self.assertIn(str(scope), filesystem["allowWrite"])
            self.assertIn(str(target), filesystem["denyWrite"])
            self.assertIn(str(repo / ".git"), filesystem["denyWrite"])
            self.assertIn(str(repo / ".git-meta"), filesystem["denyWrite"])
            with mock.patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "not-forwarded", "PATH": "/bin", "HOME": str(root)},
                clear=True,
            ):
                env = scrubbed_claude_env(private)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertEqual(env["HOME"], str(root))
            self.assertEqual(env["PATH"], "/bin")
            self.assertEqual(env["TMPDIR"], str(private))

    def test_catalogued_mcp_grant_is_the_only_mcp_available(self):
        settings = default_settings()
        settings["providers"]["claude"]["mode"] = "manual_only"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            server = repo / "tools" / "visual-server"
            server.parent.mkdir(parents=True)
            server.write_text("fixture")
            resource = repo / "fixtures" / "input.json"
            resource.parent.mkdir()
            resource.write_text("{}")
            catalog = repo / ".seats" / "worker-mcp.json"
            catalog.parent.mkdir()
            catalog.write_text(
                json.dumps(
                    {
                        "servers": {
                            "visual": {
                                "command": "tools/visual-server",
                                "args": ["fixtures/input.json", "--stdio"],
                                "env": {"MODE": "test"},
                            },
                            "ungranted": {"command": "other-server"},
                        }
                    }
                )
            )
            captured: dict[str, object] = {}
            cleaned: list[Path] = []

            def successful(command, output, *_args, **_kwargs):
                captured["command"] = command
                captured["private_temp"] = Path(
                    _args[2]["CODEX_SEATS_PRIVATE_TEMP"]
                )
                mcp_path = Path(command[command.index("--mcp-config") + 1])
                settings_path = Path(command[command.index("--settings") + 1])
                captured["launch_paths"] = {
                    "settings": settings_path,
                    "mcp": mcp_path,
                    "schema": settings_path.parent / "result-schema.json",
                }
                captured["mcp"] = json.loads(mcp_path.read_text())
                captured["settings"] = json.loads(settings_path.read_text())
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
                "prompt": "inspect",
                "kind": "review",
                "size": "small",
                "_capacity_checked": True,
                "cd": str(repo),
                "write_scope": [],
                "external_write_scope": [],
                "mcp": ["visual"],
            }
            with (
                mock.patch(
                    "codex_seats.workers.resolve_executable",
                    return_value="/fake/claude",
                ),
                mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                mock.patch("codex_seats.workers._execute", side_effect=successful),
                mock.patch(
                    "codex_seats.provider_claude.shutil.rmtree",
                    side_effect=lambda path, **_kwargs: cleaned.append(Path(path)),
                ),
            ):
                result = run_claude(root, settings, request)

            self.assertEqual(result.status, "completed")
            self.assertEqual(
                captured["mcp"],
                {
                    "mcpServers": {
                        "visual": {
                            "command": str(server.resolve()),
                            "args": [str(resource.resolve()), "--stdio"],
                            "env": {"MODE": "test"},
                        }
                    }
                },
            )
            command = captured["command"]
            tools = command[command.index("--tools") + 1].split(",")
            self.assertEqual(tools, ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "ToolSearch"])
            generated = captured["settings"]
            self.assertIn("ToolSearch", generated["permissions"]["allow"])
            self.assertIn("mcp__visual", generated["permissions"]["allow"])
            self.assertNotIn("mcp__ungranted", generated["permissions"]["allow"])
            self.assertEqual(generated["enabledMcpjsonServers"], ["visual"])
            self.assertEqual(generated["disabledMcpjsonServers"], [])
            self.assertTrue(generated["sandbox"]["failIfUnavailable"])
            self.assertFalse(generated["sandbox"]["allowUnsandboxedCommands"])
            self.assertIn("--strict-mcp-config", command)
            self.assertEqual(command[command.index("--setting-sources") + 1], "")
            launch_paths = captured["launch_paths"]
            self.assertEqual(
                {path.parent for path in launch_paths.values()},
                {next(iter(launch_paths.values())).parent},
            )
            launch_parent = next(iter(launch_paths.values())).parent
            self.assertEqual(launch_parent.parent, root.resolve() / "worker-launches")
            self.assertEqual(
                json.loads(command[command.index("--json-schema") + 1]),
                json.loads(result_schema_json()),
            )
            self.assertEqual(cleaned, [captured["private_temp"], launch_parent])

    def test_invalid_mcp_catalog_fails_before_claude_launch(self):
        settings = default_settings()
        settings["providers"]["claude"]["mode"] = "manual_only"
        cases = {
            "unknown": (
                {"servers": {"visual": {"command": "visual-server"}}},
                ["missing"],
                "not in the repository catalog",
            ),
            "malformed": ("{", ["visual"], "catalog is malformed"),
            "secret": (
                {
                    "servers": {
                        "visual": {
                            "command": "visual-server",
                            "env": {"API_TOKEN": "secret"},
                        }
                    }
                },
                ["visual"],
                "secret-bearing env keys",
            ),
        }
        for name, (payload, grants, message) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo = root / "repo"
                catalog = repo / ".seats" / "worker-mcp.json"
                catalog.parent.mkdir(parents=True)
                catalog.write_text(payload if isinstance(payload, str) else json.dumps(payload))
                request = {
                    "prompt": "inspect",
                    "_capacity_checked": True,
                    "cd": str(repo),
                    "mcp": grants,
                }
                with (
                    mock.patch("codex_seats.workers.resolve_executable") as resolve,
                    mock.patch("codex_seats.workers._execute") as execute,
                ):
                    result = run_claude(root, settings, request)
                self.assertEqual(result.status, "blocked_needs_controller")
                self.assertIn(message, result.message)
                resolve.assert_not_called()
                execute.assert_not_called()

    def test_hard_off_does_not_read_mcp_catalog(self):
        settings = default_settings()
        settings["providers"]["claude"]["mode"] = "off"
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_seats.provider_claude.resolved_worker_mcp_specs"
        ) as resolve:
            result = run_claude(Path(directory), settings, {
                "prompt": "inspect", "cd": directory, "mcp": ["missing"],
            })
        self.assertEqual(result.status, "disabled")
        resolve.assert_not_called()

    def test_safe_model_fallback_and_contained_failures(self):
        settings = default_settings()
        settings["providers"]["claude"]["mode"] = "manual_only"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            (repo / "src").mkdir(parents=True)
            target = repo / "src" / "work.txt"
            target.write_text("base")
            commands: list[list[str]] = []
            execution_cwds: list[str] = []

            def safe_execute(command, output, *_args, **_kwargs):
                commands.append(command)
                execution_cwds.append(_args[1])
                model = command[command.index("--model") + 1]
                if "fable" in model:
                    payload = {"type": "result", "subtype": "error", "is_error": True,
                               "result": "reached fable limit"}
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps(payload))
                    return "failed", json.dumps(payload), 1, 1
                payload = {
                    "type": "result", "subtype": "success", "is_error": False,
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                    "structured_output": {
                        "status": "completed", "summary": "done", "changed_files": [],
                        "test_evidence": ["fixture"], "warnings": [], "needs_controller": None,
                    },
                }
                output.write_text(json.dumps(payload))
                return "completed", json.dumps(payload), 0, 1

            request = {
                "prompt": "audit", "kind": "security", "size": "big",
                "_capacity_checked": True, "cd": str(repo),
                "write_scope": [{"path": "src", "type": "tree"}],
                "external_write_scope": [],
            }
            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/claude"),
                mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                mock.patch("codex_seats.workers._execute", side_effect=safe_execute),
            ):
                result = run_claude(root, settings, request)
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.model, "claude-opus-5-5")
            self.assertEqual(len(commands), 2)
            self.assertIn("--strict-mcp-config", commands[-1])
            self.assertIn("--json-schema", commands[-1])
            self.assertIn("--disable-slash-commands", commands[-1])
            self.assertIn("--disallowed-tools", commands[-1])
            self.assertEqual(
                commands[-1][commands[-1].index("--tools") + 1],
                "Bash,Edit,Write,Read,Glob,Grep",
            )
            self.assertEqual(execution_cwds[-1], str(repo.resolve()))

            # A failed attempt that also touched files is contained in the disposable
            # worktree (supervise/goals or the isolated one-shot run), so it stays safe
            # to fall back — the old in-place blocked_needs_controller handoff is gone.
            def changed_then_failed(command, output, *_args, **_kwargs):
                target.write_text("partial")
                payload = {"type": "result", "subtype": "error", "is_error": True,
                           "result": "reached fable limit"}
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(payload))
                return "failed", json.dumps(payload), 1, 1

            with (
                mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/claude"),
                mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
                mock.patch("codex_seats.workers._execute", side_effect=changed_then_failed),
            ):
                contained = run_claude(root, settings, request)
            self.assertNotEqual(contained.status, "blocked_needs_controller")
            self.assertTrue(contained.fallback_safe)


class TelemetrySchemaTests(unittest.TestCase):
    def test_current_legacy_and_malformed_are_distinct(self):
        current = {
            "telemetry_schema": "codex-seat.job.v5.3",
            "input_tokens": 8,
            "cached_input_tokens": 3,
            "uncached_input_tokens": 5,
            "output_tokens": 2,
            "reasoning_tokens": 1,
            "provider_tokens": 10,
        }
        self.assertEqual(validate_job_tokens(current), [])
        self.assertEqual(classify_job_telemetry(current), "current")
        self.assertEqual(classify_job_telemetry({"tokens": 10}), "legacy")
        malformed = dict(current, provider_tokens=-1)
        self.assertEqual(classify_job_telemetry(malformed), "malformed-current")


if __name__ == "__main__":
    unittest.main()
