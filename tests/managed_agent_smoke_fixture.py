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


def main() -> int:
    codex = shutil.which("codex")
    if not codex:
        print(json.dumps({"status": "blocked", "reason": "codex executable missing"}))
        return 2
    with tempfile.TemporaryDirectory(prefix="managed-agent-smoke-") as directory:
        root = Path(directory).resolve()
        repository = root / "repo"
        home = root / "home"
        private = root / "private"
        repository.mkdir()
        home.mkdir()
        private.mkdir()
        (repository / "sample.txt").write_text("alpha\nbeta\n")
        (repository / "tiny.py").write_text("VALUE = 1\n")
        (repository / "test_smoke.py").write_text(
            "import unittest\n\n"
            "class SmokeTest(unittest.TestCase):\n"
            "    def test_fixture(self):\n"
            "        self.assertEqual(1 + 1, 2)\n"
        )
        subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.name", "Smoke"], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.email", "smoke@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-qm", "base"], check=True)
        profile = worker_permission_profile(
            "job",
            repository,
            [{"path": ".", "type": "tree"}],
            [],
            private,
            generic_temp=root / "generic",
            home=home,
        )
        (home / "config.toml").write_text(profile)
        commands = [
            ["/bin/pwd"],
            ["/bin/ls", "-la"],
            ["/bin/cat", "sample.txt"],
            ["/usr/bin/wc", "-l", "sample.txt"],
            ["/usr/bin/head", "-n", "1", "sample.txt"],
            ["/usr/bin/tail", "-n", "1", "sample.txt"],
            ["/usr/bin/stat", "-f", "%N", "sample.txt"],
            ["/usr/bin/find", ".", "-maxdepth", "1", "-type", "f"],
            ["/usr/bin/env", "true"],
            ["/usr/bin/git", "status", "--short"],
            ["/usr/bin/touch", "one.txt"],
            ["/bin/mkdir", "generated"],
            ["/bin/sh", "-c", "printf shell > generated/shell.txt"],
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('generated/python.txt').write_text('python')",
            ],
            ["/bin/cp", "sample.txt", "generated/copy.txt"],
            ["/bin/mv", "generated/copy.txt", "generated/moved.txt"],
            ["/bin/chmod", "600", "generated/moved.txt"],
            ["/bin/rm", "one.txt"],
            ["/usr/bin/sed", "-i", "", "s/VALUE = 1/VALUE = 2/", "tiny.py"],
            ["/usr/bin/truncate", "-s", "4", "generated/moved.txt"],
            [sys.executable, "--version"],
            ["/usr/bin/git", "--version"],
            ["/usr/bin/env", "node", "--version"],
            ["/usr/bin/env", "jq", "--version"],
            ["/bin/sh", "-c", ":"],
            [sys.executable, "-m", "unittest", "-q", "test_smoke"],
            [sys.executable, "-m", "compileall", "-q", "."],
            [sys.executable, "-c", "import json; assert json.loads('{}') == {}"],
            ["/bin/test", "-f", "sample.txt"],
            ["/usr/bin/git", "diff", "--check"],
            ["/usr/bin/git", "status", "--short"],
            ["/usr/bin/shasum", "-a", "256", "sample.txt"],
            ["/usr/bin/sort", "sample.txt"],
        ]
        results = []
        for index, command in enumerate(commands, start=1):
            result = subprocess.run(
                [codex, "sandbox", "-P", "job", "-C", str(repository), *command],
                env={**os.environ, "CODEX_HOME": str(home), "TMPDIR": str(private)},
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
            results.append(
                {
                    "index": index,
                    "command": command,
                    "returncode": result.returncode,
                    "diagnostic": (result.stderr or result.stdout)[-200:],
                }
            )
        nested = all(
            item["returncode"] == 71
            and "sandbox_apply: Operation not permitted" in str(item["diagnostic"])
            for item in results
        )
        failed = [item for item in results if item["returncode"] != 0]
        payload = {
            "status": "blocked_nested_sandbox" if nested else (
                "completed" if not failed else "failed"
            ),
            "commands": len(commands),
            "approval_prompts": 0,
            "reviewer_events": 0,
            "guardian_events": 0,
            "controller_interventions": 0,
            "failures": failed,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2 if nested else (0 if not failed else 1)


if __name__ == "__main__":
    raise SystemExit(main())
