from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from .state import atomic_write_json
from .scope import scope_allows


RECEIPT_BYTES = 4096
RECEIPT_INJECTION_TTL_SECONDS = 300
WORKER_CONTRACT = (
    "Managed worker: execute only the assigned spec; obey AGENTS.md; preserve unrelated work. "
    "No delegation/provider polling, branch switching, protected push, merge, deploy, release, "
    "production restart/data write, or production approval. Report structured evidence."
)
TASK_WRAPPER = "Read and execute the task specification at {path}; obey repository AGENTS.md."

RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "changed_files", "test_evidence", "warnings", "needs_controller"],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed", "failed", "blocked_needs_controller"],
        },
        "summary": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "test_evidence": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "needs_controller": {"type": ["string", "null"]},
    },
}

SECRET_PATTERNS = (
    "**/.codex/auth.json",
    "**/.seats/seat*/auth.json",
    "**/.codex-seats/seat*/auth.json",
    "**/.claude/.credentials.json",
    "**/.claude/credentials.json",
    "**/.ssh/id_*",
    "**/.ssh/*_key",
    "**/.aws/credentials",
    "**/.config/gcloud/**",
    "**/.azure/**",
    "**/*.pem",
    "**/*.p12",
    "**/*.pfx",
    "**/*.key",
    "**/*.keychain",
    "**/*.keychain-db",
    "**/.env",
    "**/.env.*",
)


def _normalized(path: str, cwd: str | Path = ".") -> str:
    value = Path(os.path.expanduser(path))
    if not value.is_absolute():
        value = Path(cwd) / value
    return str(value.resolve(strict=False))


def credential_path_denied(path: str, cwd: str | Path = ".") -> bool:
    normalized = _normalized(path, cwd)
    name = Path(normalized).name
    if name in {".env.example", ".env.sample", ".env.template"}:
        return False
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in SECRET_PATTERNS)


def command_secret_paths(command: str, cwd: str | Path = ".") -> list[str]:
    """Conservatively find credential paths in a shell command without executing it."""
    candidates = re.findall(r"(?:^|[\s'\"=])((?:~|/|\.{1,2}/)[^\s'\"|;&<>]+)", command)
    try:
        candidates.extend(
            token for token in shlex.split(command)
            if token not in {"cat", "sed", "head", "tail", "less", "more", "rg", "grep"}
            and not token.startswith("-")
        )
    except ValueError:
        pass
    return sorted({path for path in candidates if credential_path_denied(path, cwd)})


def write_allowed(path: str, scopes: list[Any], cwd: str | Path) -> bool:
    if scope_allows(path, scopes, cwd, legacy=True, external=False) or scope_allows(
        path, scopes, cwd, legacy=True, external=True
    ):
        return True
    # Legacy terminal specs/results retain their original glob semantics.
    # Current launch validation rejects these before a worker can be created.
    absolute = _normalized(path, cwd)
    for scope in scopes:
        if not isinstance(scope, str):
            continue
        expanded = scope if Path(scope).is_absolute() else str(Path(cwd) / scope)
        if fnmatch.fnmatchcase(absolute, str(Path(expanded).resolve(strict=False))):
            return True
    return False


def write_result_schema(path: Path) -> None:
    atomic_write_json(path, RESULT_SCHEMA)


def read_structured_result(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("status") not in {
        "completed", "failed", "blocked_needs_controller"
    }:
        return None
    for key in ("changed_files", "test_evidence", "warnings"):
        if not isinstance(value.get(key), list):
            return None
    return value


def with_release_metadata(
    result: dict[str, Any] | None,
    release_id: Any,
    protocol_version: Any = None,
) -> dict[str, Any] | None:
    """Attach trusted runner metadata after worker-controlled schema validation."""
    if result is None:
        return None
    enriched = dict(result)
    if isinstance(release_id, str):
        enriched["release_id"] = release_id
    if isinstance(protocol_version, int) and not isinstance(protocol_version, bool):
        enriched["protocol_version"] = protocol_version
    return enriched
