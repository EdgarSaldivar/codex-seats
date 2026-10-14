from __future__ import annotations

import json

import pytest

from codex_seats.worker_hook import _simple_write_paths, worker_decision


@pytest.mark.parametrize("command, expected", [
    ("mkdir cache; swiftc -typecheck -sdk /outside/SDK touch.swift", ["cache"]),
    ("mkdir -p cache && swiftc -print-target-info -sdk /outside/SDK", ["cache"]),
    ("mkdir cache || swiftc -typecheck -module-cache-path cache rm.swift", ["cache", "cache"]),
    ("cp /outside/source local", ["local"]),
    ("cp source /outside/destination", ["/outside/destination"]),
    ("mv /outside/source local", ["/outside/source", "local"]),
])
def test_literal_paths(command, expected):
    assert _simple_write_paths(command) == expected


@pytest.mark.parametrize("command", [
    "mkdir cache; (touch /outside/bad)",
    "mkdir cache; swiftc -typecheck -sdk /outside/SDK touch.swift >out",
    "cp source /outside/bad $(true)", "cp source /outside/bad {fd}>/dev/null",
    "cp source $'/outside/bad'", 'cp source $"/outside/bad"',
    "cp source /outside/bad --backup -S .bak", "cp -at /outside source",
    "cp source ٢>/dev/null", "cp source destination\u00a0#suffix",
    "mkdir cache; sh -o errexit -c 'touch /outside/bad'",
    "xargs cat <<< /control/private-tmp/peer/file",
    "cat > note.txt <<'EOF'\nDon't panic\nEOF",
    "mkdir cache; swiftc -typecheck -sdk /outside/SDK -o output source.swift",
    "mkdir cache; swiftc -typecheck -sdk", "mkdir cache; swiftc -emit-executable source.swift",
    "mkdir cache; swiftc -typecheck @response", "mkdir cache; eval command",
    "mkdir cache; cd other; cp source destination", "mkdir cache;",
    "mkdir cache & cp source destination", "mkdir cache | cp source destination",
    "mkdir cache\ncp source destination", "mkdir cache; swiftc -typecheck 'touch.swift'",
    "mkdir cache; swiftc -typecheck touch.swift # comment\ue100",
    "mkdir cache; swiftc -typecheck touch.swift \\\n next.swift",
    "cp source -destination", "mkdir -- destination", "mkdir -p",
])
def test_complex_commands_retain_legacy_path(command):
    assert _simple_write_paths(command) is None


def test_simple_hook_success_and_boundaries(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    env = {
        "CODEX_SEATS_MANAGED_WORKER": "1",
        "CODEX_SEATS_TASK_ROOT": str(root),
        "CODEX_SEATS_WRITE_SCOPE": json.dumps([{"path": ".", "type": "tree"}]),
        "CODEX_SEATS_PROTECTED_PATHS": json.dumps(["protected.swift"]),
    }

    def decision(command):
        return worker_decision({"tool_name": "Bash", "cwd": str(root),
                                "tool_input": {"command": command}}, env)

    assert decision("mkdir cache; swiftc -typecheck -sdk /outside/SDK touch.swift") is None
    assert decision("cp /outside/source local") is None
    for command in (
        "mkdir /outside/cache; swiftc -typecheck touch.swift",
        "cp source /outside/destination", "mv /outside/source local",
        "cp source protected.swift", "mv protected.swift local",
        "mkdir cache; swiftc -typecheck -module-cache-path /outside/cache source.swift",
    ):
        result = decision(command)
        assert result is not None, command
        assert result["decision"] == "block", command
