#!/usr/bin/env python3
"""Scan tracked regular files for high-confidence credential signatures."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from codex_seats import bounded_process  # noqa: E402


PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub token": re.compile(rb"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{36,255}\b"),
    "GitHub fine-grained token": re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{70,}\b"),
    "Slack token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "OpenAI project key": re.compile(rb"\bsk-proj-[A-Za-z0-9_-]{40,}\b"),
    "OpenAI service account key": re.compile(rb"\bsk-svcacct-[A-Za-z0-9_-]{40,}\b"),
    "Anthropic key": re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{40,}\b"),
}


def tracked_files() -> list[Path]:
    git = ["git"]
    if not Path(".git").exists() and Path(".git-meta").is_dir():
        git.extend(["--git-dir=.git-meta", "--work-tree=."])
    output = bounded_process.run(
        [*git, "ls-files", "-z"], timeout=30, check=True,
    ).stdout
    return [Path(item.decode("utf-8", "strict")) for item in output.split(b"\0") if item]


def main() -> int:
    findings: list[str] = []
    scanned = 0
    for path in tracked_files():
        if path.is_symlink() or not path.is_file():
            continue
        payload = path.read_bytes()
        if b"\0" in payload:
            continue
        scanned += 1
        for label, pattern in PATTERNS.items():
            if pattern.search(payload):
                findings.append(f"{path}: {label}")
    if findings:
        print("likely-secret scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print(f"likely-secret scan passed: {scanned} tracked text files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
