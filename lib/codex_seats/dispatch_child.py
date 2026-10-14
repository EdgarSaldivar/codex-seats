"""Pinned-release child entry point for one dispatch task.

The parent runner starts this module with an explicit immutable ``lib`` path.  The
release id is supplied independently in the trusted child environment and must match
the internal request; a queued prompt cannot select code or an environment.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .bounded_process import ProcessCancelledError
from .releases import ReleaseError, release_path, validate_release_id
from .state import load_settings
from .worktree_task import TaskArtifact, run_isolated_task

MAX_CHILD_FRAME_BYTES = 64 * 1024


def _cancel_on_sigterm(_signum: int, _frame: Any) -> None:
    """Turn managed cancellation into normal lifecycle unwinding."""
    raise ProcessCancelledError("dispatch task cancellation requested")


def artifact_record(artifact: TaskArtifact) -> dict[str, Any]:
    result = asdict(artifact.result)
    return {
        "result": result,
        "changed_files": list(artifact.changed_files),
        "diff_text": artifact.diff_text,
        "worktree_path": (
            str(artifact.worktree_path) if artifact.worktree_path is not None else None
        ),
        "base": artifact.base,
        "branch": artifact.branch,
        "retained": bool(artifact.retained),
    }


def execute_from_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != {"request", "retain"}:
        raise ValueError("invalid pinned child frame")
    request = record["request"]
    if not isinstance(request, dict):
        raise ValueError("pinned child request must be an object")
    root_value = os.environ.get("CODEX_SEATS_ROOT")
    pinned = validate_release_id(os.environ.get("CODEX_SEATS_RELEASE_ID"))
    if not root_value:
        raise ReleaseError("CODEX_SEATS_ROOT is required")
    root = Path(root_value).expanduser().absolute()
    release_path(root, pinned)
    if request.get("release_id") != pinned:
        raise ReleaseError("internal request cannot override the pinned release")
    request = dict(request)
    request["release_id"] = pinned
    settings, _warnings = load_settings(root)
    artifact = run_isolated_task(
        root, settings, request, retain=record["retain"] is True
    )
    return artifact_record(artifact)


def main() -> int:
    signal.signal(signal.SIGTERM, _cancel_on_sigterm)
    data = sys.stdin.buffer.read(MAX_CHILD_FRAME_BYTES + 1)
    if len(data) > MAX_CHILD_FRAME_BYTES:
        print("pinned child frame exceeds size cap", file=sys.stderr)
        return 2
    try:
        record = json.loads(data)
        result = execute_from_record(record)
    except (OSError, ValueError, ReleaseError) as exc:
        print(f"pinned child refused: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
