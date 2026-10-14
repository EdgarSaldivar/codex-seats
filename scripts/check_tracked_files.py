#!/usr/bin/env python3
"""Fail when Git tracks pool runtime state or private material."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from pathlib import PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from codex_seats import bounded_process  # noqa: E402


ALLOWED_ROOT_FILES = {
    ".gitignore",
    "AGENTS.md",
    "LICENSE",
    "NOTICE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    ".seats-gates.json",
    "README.md",
    "claude-statusline.py",
    "codex-seat",
    "pyproject.toml",
    "uv.lock",
    "worker-hook",
}
ALLOWED_TREES = {
    "bootstrap",
    "docs",
    "examples",
    "lib",
    "scripts",
    "skills",
    "templates",
    "tests",
}
FORBIDDEN_PARTS = {
    ".cache",
    ".ruff_cache",
    "__pycache__",
    "approvals",
    "backups",
    "handoffs",
    "homes",
    "ledgers",
    "locks",
    "logs",
    "private-tmp",
    "receipts",
    "reviews",
    "reservations",
    "runs",
    "scratch",
    "seats",
    "staging",
    "usage",
}
FORBIDDEN_NAMES = {
    ".env",
    "auth.json",
    "claude-usage.json",
    "latest-handoff.json",
    "seats.json",
    "settings.json",
}
FORBIDDEN_SUFFIXES = {
    ".key",
    ".keychain",
    ".keychain-db",
    ".p12",
    ".pem",
    ".pfx",
}


def tracked_files() -> list[str]:
    git = ["git"]
    if not Path(".git").exists() and Path(".git-meta").is_dir():
        git.extend(["--git-dir=.git-meta", "--work-tree=."])
    output = bounded_process.run(
        [*git, "ls-files", "-z"], timeout=30, check=True,
    ).stdout.decode("utf-8", "strict")
    return sorted(
        path for path in output.split("\0") if path and os.path.lexists(path)
    )


def violations(paths: list[str]) -> list[str]:
    errors: list[str] = []
    for raw in paths:
        path = PurePosixPath(raw)
        parts = set(path.parts)
        root = path.parts[0]
        if len(path.parts) == 1:
            allowed = raw in ALLOWED_ROOT_FILES
        elif root == ".github":
            allowed = (
                len(path.parts) == 3
                and (
                    (path.parts[1] == "workflows" and path.suffix in {".yml", ".yaml"})
                    or (path.parts[1] == "ISSUE_TEMPLATE" and path.suffix in {".md", ".yml", ".yaml"})
                )
            ) or (
                len(path.parts) == 2 and path.name == "PULL_REQUEST_TEMPLATE.md"
            )
        else:
            allowed = root in ALLOWED_TREES
        if not allowed:
            errors.append(f"outside allowlist: {raw}")
        if parts & FORBIDDEN_PARTS:
            errors.append(f"runtime/private path: {raw}")
        if path.name in FORBIDDEN_NAMES or path.name.startswith(".env."):
            errors.append(f"private filename: {raw}")
        if any(path.name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            errors.append(f"credential filename: {raw}")
        if root.startswith("seat") and root[4:].isdigit():
            errors.append(f"live seat home: {raw}")
    return errors


def main() -> int:
    paths = tracked_files()
    errors = violations(paths)
    if errors:
        print("tracked-file policy failed:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    print(f"tracked-file policy passed: {len(paths)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
