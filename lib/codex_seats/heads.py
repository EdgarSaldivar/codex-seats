"""Real heads for the supervisor — provider-agnostic, budget-aware execution.

A "head" is one callable the supervisor invokes per iteration to make the change
that gets acceptance passing. :func:`dispatch_head` builds one that routes
through ``router.dispatch_run``, so the head is whichever provider the goal spec
asks for (``auto`` lets the budget-aware router pick Claude or a Codex seat).
The dispatch function is injectable so this stays unit-testable without a
provider.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .supervisor import HeadResult
from .task_policy import resolve_task_policy

_CAPACITY_MESSAGE = re.compile(
    r"capacity|rate limit|usage limit|quota|currently capped|pool exhausted|"
    r"no schedulable seat|usable headroom|balance is unknown",
    re.I,
)


def dispatch_head(
    root: str | Path,
    settings: dict[str, Any],
    *,
    provider: str = "auto",
    kind: str = "implementation",
    size: str | None = None,
    timeout: int = 1800,
    seat: int | None = None,
    model: str | None = None,
    effort: str | None = None,
    mcp: tuple[str, ...] = (),
    protected_paths: tuple[str, ...] = (),
    goal_id: str | None = None,
    dispatch_fn: Callable[..., Any] | None = None,
) -> Callable[[Path, str], HeadResult]:
    """Return a head that runs one managed provider task per call in the worktree.

    ``provider`` is ``auto`` / ``claude`` / ``codex``. ``seat`` forces a specific
    Codex seat. ``model`` / ``effort``
    override the provider's default model and reasoning effort (the dev-loop's
    model router sets these). ``goal_id`` stamps every dispatched job with the
    goal's identity (label + goal_id + iteration in the job ledger) so routing
    telemetry can be joined back to goal outcomes. ``dispatch_fn`` defaults to
    ``router.dispatch_run`` (imported lazily to avoid an import cycle at load).
    """

    def _dispatch(*args: Any, **kwargs: Any) -> Any:
        fn = dispatch_fn
        if fn is None:
            from .router import dispatch_run

            return dispatch_run(*args, controller_snapshot=True, **kwargs)
        return fn(*args, **kwargs)

    # The supervisor invokes the head exactly once per iteration, so a call
    # counter IS the iteration number. If a retry loop is ever added inside an
    # iteration, thread the real iteration through instead of counting calls.
    state: dict[str, Any] = {
        "calls": 0,
        "provider": provider,
        "seat": seat,
    }

    def _head(worktree_path: Path, prompt: str) -> HeadResult:
        state["calls"] += 1
        iteration_settings = settings
        root_path = Path(root)
        if (root_path / "settings.json").exists():
            from .state import load_settings

            iteration_settings, reload_warnings = load_settings(root_path)
            # An invalid settings reload already falls back to restricted
            # defaults, but silently proceeding hides that the controller's
            # configured access (including trusted-development) was dropped.
            # Fail closed and surface the reason instead of discarding it.
            rejected = [
                warning
                for warning in reload_warnings
                if warning.startswith("invalid settings")
            ]
            if rejected:
                return HeadResult(
                    ok=False,
                    summary="settings reload rejected: " + "; ".join(rejected),
                    status="blocked_needs_controller",
                )
        policy = resolve_task_policy(
            iteration_settings, kind, size=size, effort=effort
        )
        request = {
            "prompt": prompt,
            "cd": str(worktree_path),
            "provider": state["provider"],
            "kind": kind,
            "size": policy.size,
            "capability_tier": policy.capability,
            "write_scope": ["."],
            "timeout": timeout,
        }
        if protected_paths:
            # Restricted profiles and direct-tool hooks guard these paths.
            # Trusted subprocesses share the user's OS access, so the supervisor
            # still restores acceptance inputs before independently checking them.
            request["protected_paths"] = list(protected_paths)
        if goal_id:
            request["label"] = goal_id
            request["goal_id"] = goal_id
            request["iteration"] = state["calls"]
        if state["seat"] is not None:
            request["seat"] = f"seat{state['seat']}"
        if model is not None:
            request["model"] = model
        if effort is not None:
            request["effort"] = effort
        if mcp:
            request["mcp"] = list(mcp)
        result = _dispatch(root, iteration_settings, request)
        result_status = str(getattr(result, "status", "") or "")
        result_message = str(getattr(result, "message", "") or "")
        capacity_exhausted = result_status in {
            "capacity",
            "queued",
            "unavailable",
            "reserved",
        } or (
            result_status == "failed"
            and bool(_CAPACITY_MESSAGE.search(result_message))
        )
        return HeadResult(
            ok=result_status == "completed",
            summary=result_message or result_status,
            tokens=int(getattr(result, "tokens", 0) or 0),
            # RunResult.provider is where the task actually landed after any router
            # fallback — the family the review gate must oppose, not `provider` above.
            provider=str(getattr(result, "provider", "") or ""),
            status=result_status,
            worker=str(getattr(result, "worker", "") or ""),
            capacity_exhausted=capacity_exhausted,
        )

    return _head
