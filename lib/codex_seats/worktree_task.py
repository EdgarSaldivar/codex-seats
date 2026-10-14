"""Run one managed task in a throwaway git worktree, optionally *retaining* it.

The dispatch runner uses this path to capture a reviewable artifact without duplicating
provider invocation. Provider execution stays in
``router.dispatch_run``; this module only owns worktree lifecycle + artifact capture.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .workers import RunResult


@dataclass
class TaskArtifact:
    result: RunResult
    changed_files: list[str] = field(default_factory=list)
    diff_text: str = ""
    worktree_path: Path | None = None
    base: str | None = None
    branch: str | None = None
    retained: bool = False


def run_isolated_task(
    root: Path,
    settings: dict[str, Any],
    request: dict[str, Any],
    *,
    retain: bool = False,
    worktree_name: str | None = None,
) -> TaskArtifact:
    """Create a worktree off the repo's HEAD, run the task, capture the diff.

    ``request['cd']`` is the repository path (already resolved by the caller — the runner
    resolves it from the trusted project registry, never from the request). The checkout
    is placed below the controller-owned pool root, outside ordinary OS temp/cache roots.
    The whole worktree is writable (``write_scope=['.']``); a partial/failed change is
    contained in the throwaway tree. With ``retain=True`` the tree and branch survive for
    review and ``worktree_path`` is populated; otherwise both are cleaned up before
    returning and the captured diff is still available on the artifact. Raises
    ``worktree.WorktreeError`` if the repo path is not a git repository.
    """
    from . import worktree as worktree_mod
    from .router import dispatch_run

    repo = Path(request["cd"]).expanduser().resolve()
    name = worktree_name or f"task-{int(time.time())}-{os.getpid()}"
    tree = worktree_mod.create_managed(root, repo, name)
    cleaned = False
    try:
        result = dispatch_run(
            root,
            settings,
            {
                **request,
                "cd": str(tree.path),
                "write_scope": ["."],
                # Stamp controller provenance; never retain a caller's value.
                # The revision is the actual resolved base commit the created
                # worktree checked out, so downstream bindings compare against
                # what really runs.
                "_source_repository": str(repo),
                "_source_revision": str(tree.base),
            },
        )
        artifact = TaskArtifact(
            result=result,
            changed_files=tree.changed_files(),
            diff_text=tree.diff(),
            worktree_path=tree.path if retain else None,
            base=tree.base,
            branch=tree.branch,
            retained=retain,
        )
        if not retain:
            tree.remove()
            cleaned = True
        return artifact
    except Exception:
        if not cleaned:
            tree.remove()
        raise
