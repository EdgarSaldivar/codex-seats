#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from codex_seats.claude_isolation import (
    claude_job_settings,
    empty_mcp_config,
    scrubbed_claude_env,
    write_isolation_files,
)
from codex_seats.worker_hook import worker_decision
from codex_seats.worker_launch import create_worker_launch_directory
from codex_seats.write_boundary import boundary_plan


def main() -> int:
    results: list[dict[str, object]] = []

    def check(name: str, condition: bool, detail: object = None) -> None:
        results.append({"name": name, "ok": bool(condition), "detail": detail})

    with tempfile.TemporaryDirectory(prefix="claude-isolation-") as directory:
        root = Path(directory).resolve()
        repo = root / "repo"
        scope = repo / "src"
        outside = root / "outside"
        private = root / "private-tmp" / "job-one"
        private_peer = root / "private-tmp" / "job-two"
        external = root / "external"
        for path in (
            scope,
            outside,
            private,
            private_peer,
            external,
            root / "dispatch",
            root / "approvals",
        ):
            path.mkdir(parents=True)
        target = scope / "work.txt"
        target.write_text("baseline")
        hook = root / "worker-hook"
        hook.write_text("")
        launch = create_worker_launch_directory(root, "fixture")
        settings_path, mcp_path, schema_path = write_isolation_files(
            launch,
            repo,
            [{"path": "src", "type": "tree"}],
            [],
            hook,
            private_temp=private,
            protected_paths=["src/work.txt"],
            control_root=root,
        )
        settings = json.loads(settings_path.read_text())
        check(
            "launch-files-outside-scratch",
            settings_path.parent == launch and launch.parent == root / "worker-launches",
        )
        check("dont-ask", settings["permissions"]["defaultMode"] == "dontAsk")
        check("native-sandbox", settings["sandbox"] == {
            **settings["sandbox"],
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
        })
        filesystem = settings["sandbox"]["filesystem"]
        check(
            "native-filesystem-schema",
            set(filesystem) == {"allowWrite", "denyWrite", "denyRead"},
            sorted(filesystem),
        )
        check("scope-subprocess-write", str(scope) in filesystem["allowWrite"])
        check("peer-temp-native-read-deny", str(private_peer) in filesystem["denyRead"])
        check("peer-temp-native-write-deny", str(private_peer) in filesystem["denyWrite"])
        check("no-broad-additional-directories", settings["permissions"]["additionalDirectories"] == [])
        check("protected-native-deny", str(target) in filesystem["denyWrite"])
        check("git-native-deny", str(repo / ".git") in filesystem["denyWrite"])
        check("separate-git-dir-native-deny", str(repo / ".git-meta") in filesystem["denyWrite"])
        check("pool-control-native-deny", str(root / "dispatch") in filesystem["denyWrite"])
        check("approval-native-deny", str(root / "approvals") in filesystem["denyWrite"])
        check("strict-empty-mcp", json.loads(mcp_path.read_text()) == empty_mcp_config())
        check("schema-present", json.loads(schema_path.read_text())["type"] == "object")
        check("plugins-disabled", settings["enabledPlugins"] == {})
        check("agents-disabled", settings["disableAgents"] is True)
        check("session-disabled", settings["disableSessionPersistence"] is True)
        check("one-managed-hook", len(settings["hooks"]["PreToolUse"]) == 1)
        check(
            "all-filesystem-tools-hooked",
            settings["hooks"]["PreToolUse"][0]["matcher"]
            == "Bash|Edit|Write|Read|Glob|Grep",
        )
        allow = settings["permissions"]["allow"]
        check("repository-readable", any(rule.startswith("Read(") and str(repo) in rule for rule in allow))
        check("assigned-temp-readable", f"Read(/{private}/**)" in allow)
        check("scope-writable", any(rule.startswith("Edit(") and str(scope) in rule for rule in allow))
        check("outside-not-writable", not any(str(outside) in rule for rule in allow))
        deny = settings["permissions"]["deny"]
        check("credential-denies", any(("." + "env") in rule for rule in deny))
        balanced = boundary_plan(
            repo, [{"path": "src", "type": "tree"}], []
        )
        balanced_settings = claude_job_settings(
            repo,
            balanced.effective_scope,
            [{"path": str(external), "type": "tree"}],
            private,
            hook,
            control_root=root,
        )
        check("balanced-mode", balanced.mode == "balanced")
        check(
            "balanced-repo-write",
            str(repo) in balanced_settings["sandbox"]["filesystem"]["allowWrite"],
        )
        check(
            "external-exact-write",
            str(external)
            in balanced_settings["sandbox"]["filesystem"]["allowWrite"],
        )
        check(
            "external-does-not-add-parent",
            str(root)
            not in balanced_settings["sandbox"]["filesystem"]["allowWrite"],
        )
        read_only_settings = claude_job_settings(
            repo, [], [], private, hook, control_root=root
        )
        check(
            "read-only-no-repo-write",
            str(repo) not in read_only_settings["sandbox"]["filesystem"]["allowWrite"],
        )
        check(
            "read-only-private-cwd-write",
            read_only_settings["sandbox"]["filesystem"]["allowWrite"]
            == [str(private)],
        )
        git_settings = claude_job_settings(
            repo,
            [{"path": "src", "type": "tree"}],
            [],
            private,
            hook,
            allow_git_write=True,
            control_root=root,
        )
        check(
            "allow-git-write-cannot-write-metadata",
            str(repo / ".git")
            in git_settings["sandbox"]["filesystem"]["denyWrite"],
        )
        check(
            "authorized-git-keeps-controls-denied",
            str(root / "dispatch")
            in git_settings["sandbox"]["filesystem"]["denyWrite"],
        )
        with mock.patch.dict(
            os.environ,
            {"PATH": "/bin", "HOME": str(root), "TEST_API_KEY": "fixture-secret"},
            clear=True,
        ):
            env = scrubbed_claude_env(private)
        check("environment-scrubbed", "TEST_API_KEY" not in env)
        hook_env = {
            "CODEX_SEATS_MANAGED_WORKER": "1",
            "CODEX_SEATS_TASK_ROOT": str(repo),
            "CODEX_SEATS_CONTROL_ROOT": str(root),
            "CODEX_SEATS_PRIVATE_TEMP": str(private),
            "CODEX_SEATS_WRITE_SCOPE": json.dumps([{"path": "src", "type": "tree"}]),
            "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
            "CODEX_SEATS_PROTECTED_PATHS": "[]",
        }
        check(
            "direct-write-allowed",
            worker_decision(
                {"tool_name": "Write", "tool_input": {"file_path": str(target)}}, hook_env
            ) is None,
        )
        outside_decision = worker_decision(
            {"tool_name": "Write", "tool_input": {"file_path": str(outside / "no.txt")}},
            hook_env,
        )
        check("direct-write-denied", bool(outside_decision and outside_decision["decision"] == "block"))
        control_decision = worker_decision(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(root / "dispatch" / "no")},
            },
            hook_env,
        )
        check(
            "pool-control-write-denied",
            bool(control_decision and control_decision["decision"] == "block"),
        )
        production_decision = worker_decision(
            {
                "tool_name": "exec_command",
                "tool_input": {"cmd": "deploy fixture"},
            },
            hook_env,
        )
        check(
            "production-action-denied",
            bool(production_decision and production_decision["decision"] == "block"),
        )
    payload = {
        "status": "completed" if all(item["ok"] for item in results) else "failed",
        "authenticated_provider_calls": 0,
        "cases": results,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
