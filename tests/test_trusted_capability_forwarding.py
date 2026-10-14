from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from codex_seats.development_policy import TRUSTED_DEVELOPMENT_ACCESS
from codex_seats.provider_claude import run_claude
from registry_fixture import registry_settings
from codex_seats.worker_config import (
    link_portable_primary_skills,
    native_claude_mcp_names,
    portable_primary_mcp_servers,
)


def _trusted_settings() -> dict:
    settings = registry_settings()
    settings["execution"]["worker_access"] = TRUSTED_DEVELOPMENT_ACCESS
    settings["providers"]["claude"]["mode"] = "manual_only"
    return settings


def _primary_home(root: Path) -> Path:
    # The registry primary is seat1 with the default home root/seat1.
    home = root / "seat1"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _write_primary_config(home: Path, body: str) -> None:
    (home / "config.toml").write_text(body)


def _run_trusted_claude(root: Path, repository: Path, settings: dict, native_names=()) -> dict:
    """Launch run_claude with _execute mocked; return captured launch details."""
    captured: dict = {}

    def execute(command, output, *_args):
        captured["command"] = list(command)
        if "--add-dir" in command:
            add_dir = command[command.index("--add-dir") + 1]
            captured["add_dir"] = add_dir
            skills = Path(add_dir) / ".claude" / "skills"
            captured["skill_names"] = (
                sorted(p.name for p in skills.iterdir()) if skills.is_dir() else []
            )
            # os.walk with followlinks traverses the symlinked skill trees the
            # way native discovery does, capturing referenced resources.
            files: list[str] = []
            for dirpath, _dirs, filenames in os.walk(skills, followlinks=True):
                for name in filenames:
                    files.append(
                        str(Path(dirpath, name).relative_to(skills))
                    )
            captured["skill_files"] = files
        if "--mcp-config" in command:
            captured["mcp"] = json.loads(
                Path(command[command.index("--mcp-config") + 1]).read_text()
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

    with mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/claude"), \
        mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()), \
        mock.patch("codex_seats.workers._execute", side_effect=execute), \
        mock.patch("codex_seats.provider_claude.load_claude_rate_limits", return_value={}), \
        mock.patch("codex_seats.provider_claude.native_claude_mcp_names", return_value=None if native_names is None else set(native_names)):
        result = run_claude(root, settings, {
            "prompt": "p", "provider": "claude", "_capacity_checked": True,
            "cd": str(repository),
        })
    captured["result"] = result
    return captured


class PortableMcpForwardingTests(unittest.TestCase):
    def test_enabled_stdio_forwarded_disabled_and_nonportable_omitted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            (repo / "tools").mkdir(parents=True)
            (repo / "tools" / "server.js").write_text("// server\n")
            home = _primary_home(root)
            _write_primary_config(home, "\n".join([
                "[mcp_servers.context7]",
                'command = "npx"',
                'args = ["-y", "@upstash/context7-mcp"]',
                "",
                '[mcp_servers."computer-use"]',
                'command = "computer-use-server"',
                "enabled = false",
                "",
                "[mcp_servers.remote]",
                'url = "https://example.com/mcp"',
                "",
                "[mcp_servers.withsecret]",
                'command = "true"',
                'env = { API_KEY = "sekret" }',
                "",
                "[mcp_servers.localserver]",
                'command = "./tools/server.js"',
                "",
                "[mcp_servers.tuned]",
                'command = "true"',
                "startup_timeout_sec = 30",
                "",
            ]))
            settings = _trusted_settings()
            servers, warnings = portable_primary_mcp_servers(root, settings, repo)

        # Enabled stdio transports are forwarded.
        self.assertIn("context7", servers)
        self.assertNotIn("tuned", servers)
        self.assertIn("localserver", servers)
        # Disabled server stays disabled, silently (the user's explicit choice).
        self.assertNotIn("computer-use", servers)
        self.assertFalse(any("computer-use" in w for w in warnings))
        # Plain HTTP is supported natively by both providers.
        self.assertEqual(servers["remote"], {"type": "http", "url": "https://example.com/mcp"})
        # Credential-bearing env is omitted with a precise warning, not exported.
        self.assertNotIn("withsecret", servers)
        self.assertTrue(any("withsecret" in w and "credential" in w for w in warnings))
        self.assertNotIn("sekret", json.dumps(servers))
        # Project-relative command path binds to the disposable worktree.
        self.assertEqual(
            servers["localserver"]["command"], str(repo / "tools" / "server.js")
        )
        # Unrepresented fields omit the connection instead of weakening its configuration.
        self.assertTrue(any("tuned" in w and "startup_timeout_sec" in w for w in warnings))

    def test_tool_restrictions_and_environment_dependencies_are_not_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = _primary_home(root)
            _write_primary_config(home, '''
[mcp_servers.limited]
command = "true"
disabled_tools = ["publish"]
[mcp_servers.environment]
command = "true"
env_vars = ["SERVICE_TOKEN"]
''')
            servers, warnings = portable_primary_mcp_servers(root, _trusted_settings(), root)
        self.assertEqual(servers, {})
        self.assertTrue(any("disabled_tools" in warning for warning in warnings))
        self.assertTrue(any("env_vars" in warning for warning in warnings))

    def test_native_inventory_uses_bounded_cli_and_handles_failure(self):
        from codex_seats.bounded_process import ProcessResult
        result = ProcessResult((), 0, b"Checking MCP server health...\ncontext7: npx server - connected\n", b"", 1)
        with mock.patch("codex_seats.worker_config.bounded_process.run", return_value=result) as run:
            self.assertEqual(native_claude_mcp_names("/fake/claude", Path("/repo")), {"context7"})
            self.assertEqual(run.call_args.kwargs["timeout"], 20)
        with mock.patch("codex_seats.worker_config.bounded_process.run", return_value=ProcessResult((), 1, b"", b"failed", 1)):
            self.assertIsNone(native_claude_mcp_names("/fake/claude", Path("/repo")))

    def test_missing_command_dependency_is_reported(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            home = _primary_home(root)
            _write_primary_config(home, "\n".join([
                "[mcp_servers.absent]",
                'command = "definitely-not-a-real-binary-xyz"',
                "",
            ]))
            servers, warnings = portable_primary_mcp_servers(
                root, _trusted_settings(), repo
            )
        # Still forwarded (visible), but the missing dependency is flagged.
        self.assertIn("absent", servers)
        self.assertTrue(any("absent" in w and "not on PATH" in w for w in warnings))

    def test_no_primary_config_yields_nothing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            servers, warnings = portable_primary_mcp_servers(
                root, _trusted_settings(), repo
            )
        self.assertEqual(servers, {})
        self.assertEqual(warnings, [])


class PortableSkillLinkTests(unittest.TestCase):
    def test_system_skills_keep_resources_and_explicit_disables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = _primary_home(root)
            for name in ("available", "disabled"):
                skill = home / "skills" / ".system" / name
                (skill / "scripts").mkdir(parents=True)
                (skill / "SKILL.md").write_text("use scripts/check.py")
                (skill / "scripts" / "check.py").write_text("print('ok')")
            _write_primary_config(home, '[[skills.config]]\npath = "skills/.system/disabled/SKILL.md"\nenabled = false\n')
            caps = root / "caps"
            link_portable_primary_skills(root, _trusted_settings(), root, caps)
            self.assertTrue((caps / ".claude/skills/available/scripts/check.py").is_file())
            self.assertFalse((caps / ".claude/skills/disabled").exists())

    def test_links_complete_tree_and_skips_native_collision(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            home = _primary_home(root)
            portable = home / "skills" / "portabledemo"
            (portable / "scripts").mkdir(parents=True)
            (portable / "SKILL.md").write_text("use scripts/check.sh")
            (portable / "scripts" / "check.sh").write_text("#!/bin/sh\n")
            # A native project skill of the same name must win the collision.
            collide = home / "skills" / "collider"
            collide.mkdir(parents=True)
            (collide / "SKILL.md").write_text("primary collider")
            (repo / ".claude" / "skills" / "collider").mkdir(parents=True)
            (repo / ".claude" / "skills" / "collider" / "SKILL.md").write_text("native")

            caps = root / "caps"
            caps.mkdir()
            skills_root, warnings = link_portable_primary_skills(
                root, _trusted_settings(), repo, caps
            )

            self.assertEqual(skills_root, caps)
            linked = caps / ".claude" / "skills"
            # The whole referenced tree is reachable through the link.
            self.assertTrue((linked / "portabledemo" / "scripts" / "check.sh").exists())
            self.assertTrue((linked / "portabledemo" / "SKILL.md").exists())
            # The colliding portable skill is skipped and the difference is surfaced.
            self.assertFalse((linked / "collider").exists())
            self.assertTrue(any("collider" in w and "native" in w for w in warnings))

    def test_absent_primary_skills_return_none(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            _primary_home(root)  # no skills/ subtree
            caps = root / "caps"
            caps.mkdir()
            skills_root, warnings = link_portable_primary_skills(
                root, _trusted_settings(), repo, caps
            )
        self.assertIsNone(skills_root)
        self.assertEqual(warnings, [])


class TrustedLaunchForwardingTests(unittest.TestCase):
    def test_referenced_skill_script_and_enabled_mcp_reach_the_launch(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            home = _primary_home(root)
            skill = home / "skills" / "portabledemo"
            (skill / "scripts").mkdir(parents=True)
            (skill / "SKILL.md").write_text("use scripts/check.sh")
            (skill / "scripts" / "check.sh").write_text("#!/bin/sh\necho ok\n")
            _write_primary_config(home, "\n".join([
                "[mcp_servers.context7]",
                'command = "npx"',
                'args = ["-y", "@upstash/context7-mcp"]',
                "",
                '[mcp_servers."computer-use"]',
                'command = "cu"',
                "enabled = false",
                "",
                "[mcp_servers.remote]",
                'url = "https://example.com/mcp"',
                "",
            ]))
            captured = _run_trusted_claude(root, repo, _trusted_settings())

        result = captured["result"]
        self.assertEqual(result.status, "completed", result.message)
        command = captured["command"]
        # Native --add-dir discovery of the linked .claude/skills tree.
        self.assertIn("--add-dir", command)
        self.assertIn("portabledemo", captured["skill_names"])
        self.assertIn("portabledemo/scripts/check.sh", captured["skill_files"])
        # Enabled portable MCP forwarded into the additive per-job config;
        # the disabled server stays disabled.
        self.assertIn("--mcp-config", command)
        forwarded = captured["mcp"]["mcpServers"]
        self.assertIn("context7", forwarded)
        self.assertNotIn("computer-use", forwarded)
        self.assertEqual(forwarded["remote"]["type"], "http")

    def test_native_mcp_collision_and_inventory_failure_preserve_native_configuration(self):
        for native_names in ({"context7"}, None):
            with self.subTest(native_names=native_names), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                home = _primary_home(root)
                _write_primary_config(home, '[mcp_servers.context7]\ncommand = "true"\n')
                captured = _run_trusted_claude(root, root, _trusted_settings(), native_names)
                self.assertNotIn("--mcp-config", captured["command"])
                warnings = captured["result"].structured_result["warnings"]
                self.assertTrue(any("native" in warning.lower() for warning in warnings))

    def test_restricted_launch_forwards_nothing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            home = _primary_home(root)
            skill = home / "skills" / "portabledemo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("skill")
            _write_primary_config(home, "\n".join([
                "[mcp_servers.context7]",
                'command = "npx"',
                "",
            ]))
            settings = registry_settings()  # restricted
            settings["providers"]["claude"]["mode"] = "manual_only"
            captured = _run_trusted_claude(root, repo, settings)

        result = captured["result"]
        self.assertEqual(result.status, "completed", result.message)
        command = captured["command"]
        # Restricted mode is unchanged: no skill add-dir, and the per-job MCP
        # config keeps its existing empty/strict shape (no portable forwarding).
        self.assertNotIn("--add-dir", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(captured["mcp"]["mcpServers"], {})


if __name__ == "__main__":
    unittest.main()
