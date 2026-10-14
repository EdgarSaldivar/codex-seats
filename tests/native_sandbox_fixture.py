#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from codex_seats.scope import worker_permission_profile
from codex_seats.worker_hook import worker_decision


def run_case(
    codex: str,
    home: Path,
    repository: Path,
    name: str,
    command: list[str],
    expected: bool,
) -> dict[str, object]:
    result = subprocess.run(
        [codex, "sandbox", "-P", "job", "-C", str(repository), *command],
        env={**os.environ, "CODEX_HOME": str(home)},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    passed = result.returncode == 0
    return {
        "name": name,
        "expected": "allow" if expected else "deny",
        "observed": "allow" if passed else "deny",
        "ok": passed == expected,
        "returncode": result.returncode,
        "diagnostic": (result.stderr or result.stdout)[-300:],
    }


def main() -> int:
    codex = shutil.which("codex")
    if not codex:
        print(json.dumps({"status": "blocked", "reason": "codex executable missing"}))
        return 2
    with tempfile.TemporaryDirectory(
        prefix="native-sandbox-", ignore_cleanup_errors=True
    ) as directory:
        root = Path(directory).resolve()
        repository = root / "repo"
        home = root / "home"
        external = root / "external"
        private = root / "private"
        # Match production dispatch worktrees: the repository is a child of the
        # otherwise-denied generic temp directory.
        generic = root
        outside = root / "outside"
        for path in (
            repository / "tree" / "nested",
            home / ".codex",
            home / ".claude",
            external,
            private,
            outside,
            root / "dispatch",
            root / "approvals",
        ):
            path.mkdir(parents=True)
        subprocess.run(
            ["git", "-C", str(repository), "init", "-q"],
            stdin=subprocess.DEVNULL,
            check=True,
        )
        (repository / "read.txt").write_text("readable\n")
        (repository / "allowed.txt").write_text("old\n")
        (repository / "protected.txt").write_text("dirty baseline\n")
        (outside / "protected.txt").write_text("baseline\n")
        secret_name = "." + "env"
        (repository / secret_name).write_text("secret\n")
        (repository / ".env.example").write_text("example\n")
        (repository / "tree" / "nested" / ".env.local").write_text("nested secret\n")
        (repository / "tree" / "nested" / "private.pem").write_text("private\n")
        (repository / "login.keychain-db").write_text("keychain\n")
        (repository / "secret-link").symlink_to(repository / secret_name)
        (repository / "tree" / "escape").symlink_to(outside, target_is_directory=True)
        (home / ".codex" / "auth.json").write_text("credential\n")
        (home / ".claude" / ".credentials.json").write_text("credential\n")
        (root / "dispatch" / "capability.token").write_text("control secret\n")
        (root / "approvals" / "fixture.json").write_text("approval\n")
        (root / "settings.json").write_text("{}\n")
        profile = worker_permission_profile(
            "job",
            repository,
            [
                {"path": ".", "type": "tree"},
            ],
            [{"path": str(external), "type": "tree"}],
            private,
            protected_paths=["protected.txt"],
            generic_temp=generic,
            home=home,
            control_root=root,
        )
        (home / "config.toml").write_text(profile)
        cases = [
            ("repository-read", ["/bin/cat", str(repository / "read.txt")], True),
            (
                "whole-tree-write",
                ["/bin/sh", "-c", f"printf exact > {repository / 'allowed.txt'}"],
                True,
            ),
            (
                "python-whole-tree-write",
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(repository / 'allowed.txt')!r}).write_text('python')",
                ],
                True,
            ),
            (
                "dirty-baseline-write-denied",
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(repository / 'protected.txt')!r}).write_text('no')",
                ],
                False,
            ),
            (
                "python-outside-write",
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(outside / 'python.txt')!r}).write_text('no')",
                ],
                False,
            ),
            (
                "redirection-outside",
                ["/bin/sh", "-c", f"printf no > {outside / 'redirect.txt'}"],
                False,
            ),
            (
                "formatter-outside",
                [
                    "/bin/sh",
                    "-c",
                    f"printf '{{}}' | {sys.executable} -m json.tool > {outside / 'format.json'}",
                ],
                False,
            ),
            ("delete-outside", ["/bin/rm", "-f", str(outside / "protected.txt")], False),
            (
                "symlink-traversal",
                [
                    "/bin/sh",
                    "-c",
                    f"printf no > {repository / 'tree' / 'escape' / 'traversed.txt'}",
                ],
                False,
            ),
            (
                "generic-temp-write",
                ["/usr/bin/touch", str(generic / "undeclared")],
                False,
            ),
            (
                "private-temp-write",
                ["/usr/bin/touch", str(private / "declared")],
                True,
            ),
            (
                "external-write",
                ["/usr/bin/touch", str(external / "declared")],
                True,
            ),
            (
                "git-metadata-write-denied",
                ["/usr/bin/touch", str(repository / ".git" / "managed-denied")],
                False,
            ),
            (
                "codex-auth-read-denied",
                ["/bin/cat", str(home / ".codex" / "auth.json")],
                False,
            ),
            (
                "claude-credential-read-denied",
                ["/bin/cat", str(home / ".claude" / ".credentials.json")],
                False,
            ),
            (
                "pool-control-read-denied",
                ["/bin/cat", str(root / "dispatch" / "capability.token")],
                False,
            ),
            (
                "pool-control-write-denied",
                ["/usr/bin/touch", str(root / "dispatch" / "managed-denied")],
                False,
            ),
            (
                "pool-runtime-write-denied",
                ["/usr/bin/touch", str(root / "settings.json")],
                False,
            ),
            (
                "approval-read-denied",
                ["/bin/cat", str(root / "approvals" / "fixture.json")],
                False,
            ),
            ("secret-read", ["/bin/cat", str(repository / secret_name)], False),
            (
                "whole-tree-nested-env-local",
                ["/bin/cat", str(repository / "tree" / "nested" / ".env.local")],
                False,
            ),
            (
                "whole-tree-nested-private-key",
                ["/bin/cat", str(repository / "tree" / "nested" / "private.pem")],
                False,
            ),
            (
                "whole-tree-root-keychain",
                ["/bin/cat", str(repository / "login.keychain-db")],
                False,
            ),
            ("whole-tree-secret-symlink", ["/bin/cat", str(repository / "secret-link")], False),
            ("env-example-read", ["/bin/cat", str(repository / ".env.example")], True),
        ]
        results = [
            run_case(codex, home, repository, name, command, expected)
            for name, command, expected in cases
        ]
        exact_profile = worker_permission_profile(
            "job",
            repository,
            [
                {"path": "tree/inside.txt", "type": "file"},
                {"path": ".env", "type": "file"},
                {"path": "tree/nested/.env.local", "type": "file"},
                {"path": "tree/nested/private.pem", "type": "file"},
                {"path": "login.keychain-db", "type": "file"},
                {"path": ".env.example", "type": "file"},
            ],
            [],
            private,
            protected_paths=["protected.txt"],
            generic_temp=generic,
            home=home,
            control_root=root,
        )
        (home / "config.toml").write_text(exact_profile)
        exact_cases = [
            (
                "exact-inside-write",
                ["/usr/bin/touch", str(repository / "tree" / "inside.txt")],
                True,
            ),
            (
                "exact-outside-write-denied",
                ["/usr/bin/touch", str(repository / "outside-exact")],
                False,
            ),
            ("exact-scope-root-env", ["/bin/cat", str(repository / secret_name)], False),
            (
                "exact-scope-nested-env-local",
                ["/bin/cat", str(repository / "tree" / "nested" / ".env.local")],
                False,
            ),
            (
                "exact-scope-nested-private-key",
                ["/bin/cat", str(repository / "tree" / "nested" / "private.pem")],
                False,
            ),
            (
                "exact-scope-root-keychain",
                ["/bin/cat", str(repository / "login.keychain-db")],
                False,
            ),
            ("exact-scope-secret-symlink", ["/bin/cat", str(repository / "secret-link")], False),
            ("exact-scope-env-example", ["/bin/cat", str(repository / ".env.example")], True),
        ]
        results.extend(
            run_case(codex, home, repository, name, command, expected)
            for name, command, expected in exact_cases
        )
        read_only_profile = worker_permission_profile(
            "job",
            repository,
            [],
            [],
            private,
            generic_temp=generic,
            home=home,
            control_root=root,
        )
        (home / "config.toml").write_text(read_only_profile)
        read_only_cases = [
            ("read-only-repository-read", ["/bin/cat", str(repository / "read.txt")], True),
            (
                "read-only-repository-write-denied",
                ["/usr/bin/touch", str(repository / "read-only-denied")],
                False,
            ),
            (
                "read-only-external-write-denied",
                ["/usr/bin/touch", str(external / "read-only-denied")],
                False,
            ),
            (
                "read-only-private-temp-write",
                ["/usr/bin/touch", str(private / "read-only-private")],
                True,
            ),
        ]
        results.extend(
            run_case(codex, home, repository, name, command, expected)
            for name, command, expected in read_only_cases
        )
        git_profile = worker_permission_profile(
            "job",
            repository,
            [{"path": "tree", "type": "tree"}],
            [{"path": str(external), "type": "tree"}],
            private,
            generic_temp=generic,
            home=home,
            allow_git_write=True,
            control_root=root,
        )
        (home / "config.toml").write_text(git_profile)
        git_cases = [
            (
                "allow-git-write-cannot-write-metadata",
                ["/usr/bin/touch", str(repository / ".git" / "managed-allowed")],
                False,
            ),
            (
                "authorized-git-keeps-repo-scope-exact",
                ["/usr/bin/touch", str(repository / "git-does-not-widen")],
                False,
            ),
            (
                "authorized-git-keeps-external-exact",
                ["/usr/bin/touch", str(external / "git-external-allowed")],
                True,
            ),
        ]
        results.extend(
            run_case(codex, home, repository, name, command, expected)
            for name, command, expected in git_cases
        )
        launch_parent = root / "worker-launches"
        launch = launch_parent / "fixture"
        launch.mkdir(parents=True)
        launch_config = launch / "settings.json"
        launch_config.write_text('{"synthetic": true}\n')
        # Deliberately grant the surrounding fixture root so these checks prove
        # the launch denial itself, not an unrelated generic-temp denial.
        (home / "config.toml").write_text(worker_permission_profile(
            "job", repository, ["."], [{"path": str(root), "type": "tree"}],
            private, home=home, control_root=root,
        ))
        for name, operation in (
            ("launch-config-read", f"Path({str(launch_config)!r}).read_text()"),
            ("launch-config-write", f"Path({str(launch_config)!r}).write_text('no')"),
            ("launch-directory-rename", f"os.rename({str(launch)!r}, {str(root / 'moved-job')!r})"),
            ("launch-parent-rename", f"os.rename({str(launch_parent)!r}, {str(root / 'moved-launches')!r})"),
        ):
            script = (
                "import os, sys\nfrom pathlib import Path\ntry:\n"
                f"    {operation}\nexcept PermissionError:\n    sys.exit(13)\n"
            )
            outcome = run_case(
                codex, home, repository, name,
                [sys.executable, "-c", script], False,
            )
            # Missing files or malformed commands must not masquerade as denial.
            outcome["ok"] = outcome["returncode"] == 13
            results.append(outcome)
        # Protect the link itself as well as its canonical Git target. A rule
        # for only the target permits unlinking the pointer in a writable tree.
        linked_repo = root / "symlink-git-repo"
        linked_repo.mkdir()
        subprocess.run(
            ["git", "-C", str(linked_repo), "init", "-q"], check=True,
            stdin=subprocess.DEVNULL,
        )
        linked_admin = root / "symlink-git-admin"
        (linked_repo / ".git").rename(linked_admin)
        git_link = linked_repo / ".git"
        git_link.symlink_to(linked_admin, target_is_directory=True)
        git_head_before = (linked_admin / "HEAD").read_bytes()
        (home / "config.toml").write_text(worker_permission_profile(
            "job", linked_repo, ["."], [{"path": str(root), "type": "tree"}],
            private, home=home, control_root=root,
        ))
        outcome = run_case(
            codex, home, linked_repo, "git-symlink-unlink-denied",
            [sys.executable, "-c",
             "from pathlib import Path\nimport sys\ntry:\n"
             " Path(sys.argv[1]).unlink()\nexcept PermissionError:\n"
             " raise SystemExit(13)\n", str(git_link)], False,
        )
        outcome["ok"] = (
            outcome["returncode"] == 13 and git_link.is_symlink()
            and (linked_admin / "HEAD").read_bytes() == git_head_before
        )
        results.append(outcome)
        hook_env = {
            "CODEX_SEATS_MANAGED_WORKER": "1",
            "CODEX_SEATS_TASK_ROOT": str(repository),
            "CODEX_SEATS_CONTROL_ROOT": str(root),
            "CODEX_SEATS_WRITE_SCOPE": json.dumps([{"path": ".", "type": "tree"}]),
            "CODEX_SEATS_EXTERNAL_WRITE_SCOPE": "[]",
            "CODEX_SEATS_PROTECTED_PATHS": "[]",
        }
        hook_cases = []
        for name, command in (
            ("hook-production-denied", "deploy fixture"),
            ("hook-push-denied", "git push origin main"),
            ("hook-dispatch-control-denied", "codex-seat dispatch uninstall"),
        ):
            decision = worker_decision(
                {"tool_name": "exec_command", "tool_input": {"cmd": command}},
                hook_env,
            )
            hook_cases.append(
                {
                    "name": name,
                    "expected": "deny",
                    "observed": "deny" if decision else "allow",
                    "ok": bool(decision and decision.get("decision") == "block"),
                    "returncode": None,
                    "diagnostic": decision.get("reason", "") if decision else "",
                }
            )
        nested = bool(results) and all(
            result["returncode"] == 71
            and "sandbox_apply: Operation not permitted" in str(result["diagnostic"])
            for result in results
        ) and all(result["ok"] for result in hook_cases)
        payload = {
            "status": "blocked_nested_sandbox" if nested else (
                "completed" if all(result["ok"] for result in [*results, *hook_cases]) else "failed"
            ),
            "approval_prompts": 0,
            "reviewer_events": 0,
            "guardian_events": 0,
            "cases": [*results, *hook_cases],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2 if nested else (0 if payload["status"] == "completed" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
